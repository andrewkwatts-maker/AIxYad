# GUID: a7c8f3b1-9e2d-4f5a-b8c3-2d1e9a7f6b5c
#!/usr/bin/env python3
"""
Planning Quality Gate — Validates plan completeness before execution.

After an initial planning pass, this module runs a second model call to evaluate
whether the plan is complete and specific enough to execute. If not, it performs
one round of clarification queries (reading relevant files) before execution starts.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def evaluate_plan_completeness(
    adapter,
    task: dict,
    context: dict,
    instructions: dict,
    history: List[Dict],
    permissions: dict,
    session_id: str,
    project_dir: Path,
) -> Dict[str, Any]:
    """Run a second model call to evaluate plan completeness.
    
    Returns:
        {
            'is_complete': bool,
            'missing_items': [str],  # List of missing information/files
            'clarifications_needed': [str],  # Specific questions/files to clarify
        }
    """
    evaluation_prompt = {
        'protocol': 'aixyad-agent/1.0',
        'session_id': session_id,
        'sequence': -1,  # pre-loop sentinel
        'task': task,
        'context': context,
        'instructions': {
            'step_prompt': _build_evaluation_prompt(task, history),
        },
        'history': history[-10:],  # Last 10 exchanges for context
        'permissions': permissions,
        'available_actions': [],  # No actions during evaluation
    }
    
    # Isolate this call from the agent's main message history — the gate
    # asks for {is_complete, missing, reasoning} JSON which doesn't match the
    # action protocol; if its prompt+response stay in adapter._messages the
    # next action call will see them and the model gets confused, emitting
    # is_complete shape instead of action shape and failing every iteration.
    saved_messages = getattr(adapter, '_messages', None)
    if saved_messages is not None:
        snapshot = list(saved_messages)
        try:
            adapter._messages = list(snapshot[:1])  # keep only system prompt
            response = adapter.send(evaluation_prompt)
        except Exception as exc:
            adapter._messages = snapshot
            return {
                'is_complete': True,
                'missing_items': [],
                'clarifications_needed': [],
                'error': str(exc),
            }
        finally:
            adapter._messages = snapshot
    else:
        try:
            response = adapter.send(evaluation_prompt)
        except Exception as exc:
            return {
                'is_complete': True,
                'missing_items': [],
                'clarifications_needed': [],
                'error': str(exc),
            }
    return _parse_evaluation_response(response)


def _build_evaluation_prompt(task: dict, history: List[Dict]) -> str:
    """Build the evaluation prompt asking if the plan is complete."""
    task_title = task.get('title', 'Unknown task')
    task_desc = task.get('description', '')
    acceptance_criteria = task.get('acceptance_criteria', [])

    # Iterating a string yields characters — split per line (or treat as one
    # bullet) so the prompt isn't corrupted into one-char-per-bullet.
    if not acceptance_criteria:
        criteria_text = "  (none specified)"
    elif isinstance(acceptance_criteria, list):
        items = [str(c).strip() for c in acceptance_criteria if str(c).strip()]
        criteria_text = '\n'.join(f"  - {c}" for c in items) if items else "  (none specified)"
    else:
        text = str(acceptance_criteria).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        items = lines if len(lines) > 1 else [text]
        criteria_text = '\n'.join(f"  - {c}" for c in items)
    
    # Extract the plan from recent history (look for assistant responses)
    plan_text = ""
    for entry in reversed(history[-5:]):
        if entry.get('role') == 'assistant':
            content = entry.get('content', '')
            if isinstance(content, str) and len(content) > 100:
                plan_text = content[:2000]
                break
    
    return f"""PLANNING QUALITY GATE EVALUATION

You have just created a plan for this task:

**Task Title:** {task_title}

**Description:**
{task_desc}

**Acceptance Criteria:**
{criteria_text}

**Your Plan:**
{plan_text}

Now, evaluate whether this plan is **complete and specific enough to execute**.

Ask yourself:
1. Do I know EXACTLY which files to read, edit, or create?
2. Do I have enough information about the current codebase structure?
3. Are there any dependencies, configuration files, or related code I should review first?
4. Is the approach clear and unambiguous?
5. Do I need to understand existing implementations before proceeding?

Respond with a JSON object:
{{
    "is_complete": true/false,
    "missing": [
        "Specific file path or information needed",
        "Another file or clarification needed",
        ...
    ],
    "reasoning": "Brief explanation of what's missing or why it's complete"
}}

Examples of good "missing" items:
- "src/main.py — need to see current implementation"
- "package.json — need to check existing dependencies"
- "tests/test_auth.py — need to understand current test structure"

Examples of bad "missing" items (too vague):
- "more information"
- "understand the codebase"
- "check the code"

Be specific about file paths when possible.
"""


def _parse_evaluation_response(response: Any) -> Dict[str, Any]:
    """Parse the model's evaluation response into structured data."""
    # Extract content from various response formats
    if isinstance(response, dict):
        content = (
            response.get('content') or
            response.get('text') or
            response.get('reasoning') or
            json.dumps(response)
        )
    else:
        content = str(response)
    
    # Try to extract JSON object
    result = {
        'is_complete': True,
        'missing_items': [],
        'clarifications_needed': [],
    }
    
    try:
        # Look for JSON object in the content
        json_match = re.search(r'\{[^{}]*"is_complete"[^{}]*\}', content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
        else:
            # Try parsing the entire content
            parsed = json.loads(content)
        
        result['is_complete'] = parsed.get('is_complete', True)
        
        missing = parsed.get('missing', [])
        if isinstance(missing, list):
            result['missing_items'] = missing
            result['clarifications_needed'] = missing[:5]  # Cap at 5 items
        elif isinstance(missing, str):
            result['missing_items'] = [missing]
            result['clarifications_needed'] = [missing]
        
        result['reasoning'] = parsed.get('reasoning', '')
        
    except (json.JSONDecodeError, AttributeError):
        # If we can't parse JSON, look for keywords indicating incompleteness
        content_lower = content.lower()
        if any(keyword in content_lower for keyword in ['incomplete', 'missing', 'need to', 'should read']):
            result['is_complete'] = False
            # Try to extract file paths from content
            file_paths = re.findall(r'[\w./\\-]+\.\w{2,4}', content)
            result['clarifications_needed'] = file_paths[:3]
    
    return result


def perform_clarification_round(
    evaluation: Dict[str, Any],
    project_dir: Path,
    history: List[Dict],
) -> List[str]:
    """Read files mentioned in missing items and add to history.
    
    Returns list of files that were successfully read.
    """
    if evaluation.get('is_complete', True):
        return []
    
    missing_items = evaluation.get('clarifications_needed', [])
    if not missing_items:
        return []
    
    files_read = []
    
    for item in missing_items[:3]:  # Limit to 3 files
        # Extract file path from the missing item
        file_paths = _extract_file_paths(item)
        
        for file_path in file_paths[:1]:  # One file per missing item
            content = _read_file_safe(project_dir, file_path)
            if content and not content.startswith('[Error') and not content.startswith('[File not found'):
                files_read.append(file_path)
                history.append({
                    'role': 'system',
                    'content': (
                        f'[Planning clarification] Read {file_path}:\n\n'
                        f'{content[:3000]}'
                        f'{"...[truncated]" if len(content) > 3000 else ""}'
                    ),
                })
    
    if files_read:
        history.append({
            'role': 'system',
            'content': (
                f'[Planning quality gate] Read {len(files_read)} clarification file(s): '
                f'{", ".join(files_read)}. Plan is now ready for execution.'
            ),
        })
    
    return files_read


def _extract_file_paths(text: str) -> List[str]:
    """Extract plausible file paths from a text string."""
    # Look for path-like patterns
    # Match: word chars, dots, slashes, hyphens + extension
    patterns = [
        r'[\w./\\-]+\.\w{2,4}',  # Standard file paths with extensions
        r'[\w-]+/[\w./\\-]+',     # Paths with directory separators
    ]
    
    paths = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        paths.extend(matches)
    
    # Also check if the entire text (stripped) looks like a path
    stripped = text.strip()
    if '/' in stripped or '\\' in stripped or '.' in stripped:
        paths.append(stripped)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_paths = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)
    
    return unique_paths


def _read_file_safe(project_dir: Path, file_path: str) -> str:
    """Safely read a file, returning content or error message."""
    try:
        # Try as relative path first
        full_path = project_dir / file_path
        if full_path.is_file():
            return full_path.read_text(encoding='utf-8', errors='replace')
        
        # Try as absolute path
        abs_path = Path(file_path)
        if abs_path.is_file():
            return abs_path.read_text(encoding='utf-8', errors='replace')
        
        return f"[File not found: {file_path}]"
    
    except Exception as exc:
        return f"[Error reading {file_path}: {exc}]"
