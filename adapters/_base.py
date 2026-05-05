#!/usr/bin/env python3
"""
Base adapter infrastructure — shared by all AI provider adapters.

Contains:
  VALID_ACTIONS   — known action names accepted by the agent executor
  SYSTEM_PROMPT   — universal JSON-protocol instruction injected into every request
  JSON helpers    — extraction / repair utilities for small-model outputs
  BaseAdapter     — abstract base class with prompt building + response parsing
  _error_response — factory for well-formed error responses
"""

import json
import re
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Known valid action names
# ---------------------------------------------------------------------------

VALID_ACTIONS = frozenset([
    'read_file', 'write_file', 'edit_file', 'search', 'list_files',
    'run_command', 'run_script', 'commit', 'step_done', 'need_help',
    # DB-backed task actions
    'query_tasks', 'query_task_detail', 'update_task', 'add_task',
    'reorder_tasks', 'add_checklist_item', 'check_item',
    'query_sprint', 'query_sprint_summary', 'query_history',
    'query_task_timeline', 'link_file_to_task', 'log_file_modification',
    # Task reservation and assignment
    'reserve_task', 'release_task', 'assign_task', 'reserve_subtask',
    # Safe delete / discard / restore
    'discard_task', 'restore_task', 'discard_file', 'restore_file',
    'query_discarded_files', 'query_file_history',
    # Live scanner actions
    'query_file_structure', 'query_symbols', 'query_files', 'query_file_section',
    # GUID code section tracking
    'create_section', 'edit_section', 'find_section', 'get_section_history',
    # File locking
    'lock_file', 'unlock_file', 'check_file_lock',
    # Prompt logs
    'query_prompt_logs',
    # Config / agent definition accessors
    'get_config', 'set_config', 'get_agent_def',
    # Vision / UI automation
    'screenshot', 'screenshot_window', 'screenshot_region',
    'analyze_image', 'analyze_screenshot',
    'ui_click', 'ui_type', 'ui_key_press', 'ui_scroll', 'ui_drag', 'ui_type_in_field',
    'launch_app', 'get_window_info', 'list_windows', 'bring_window_to_front',
    'get_screen_size', 'get_mouse_position',
    # View / navigation actions
    'get_view', 'set_view', 'get_view_compact',
    'get_related_content', 'get_related_keywords', 'set_related_keywords',
    'replace_section', 'insert_line', 'remove_section', 'rename_symbol',
])


# ---------------------------------------------------------------------------
# Universal system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an AI software agent operating via a structured JSON protocol.
You MUST respond with ONLY a single JSON object. No markdown, no code blocks, no explanation outside the JSON.
Choose exactly one action from the available_actions list in the request.

Response format (respond with exactly this structure):
{"action": "action_name", "params": {...}, "reasoning": "brief reason"}

Core actions:
- "read_file": Read a file. Params: {"path": "relative/path.py"}
- "write_file": Create or overwrite a file. ALWAYS include overwrite:true. Params: {"path": "relative/path.py", "content": "...", "overwrite": true}
- "edit_file": Replace EXACT text in existing file. Params: {"path": "relative/path.py", "old_text": "exact text", "new_text": "replacement"}
- "list_files": List files. Params: {"pattern": "src/**/*.py"}
- "run_command": Run shell command. Params: {"command": "python -m py_compile src/main.py"}
- "step_done": Task complete. Call this ONCE all criteria are met. Params: {"summary": "what was done"}
- "need_help": Stuck or blocked. Params: {"reason": "why stuck"}

CRITICAL WORKFLOW — follow exactly:
1. Read the task description carefully.
2. EXPLORE FIRST: call list_files or read_file to understand the EXISTING codebase before making any changes.
   - NEVER invent file paths. Only use paths you have confirmed exist via list_files or read_file.
   - For modification tasks: read the target file first, then use edit_file (not write_file) to change it.
3. Perform each required action ONE AT A TIME (edit file, run command, etc.).
4. After each SUCCESS, mentally tick off that criterion. Do NOT redo completed steps.
5. Once ALL acceptance criteria are satisfied → call step_done IMMEDIATELY. Do not do extra work.
6. If an action fails TWICE with the same error → call need_help, do not retry endlessly.

EXPLORE-BEFORE-MODIFY RULES (critical for working in an existing codebase):
- ALWAYS start with list_files("**/*.py") or read_file on the specific file before writing anything.
- NEVER call write_file on a path you haven't confirmed exists (or is genuinely new).
- Prefer edit_file over write_file for ALL changes to existing files.
- If the task says "add X to file Y.py" → read Y.py first, then edit_file to insert X.

FILE WRITE RULES:
- ALWAYS include "overwrite": true in write_file params. Files may already exist from previous runs.
- Use relative paths only (e.g. "sprint_runner.py", not "H:/Github/project/sprint_runner.py").
- Do NOT read a file you just successfully wrote — you already know what's in it.
- If read_file returns FILE NOT FOUND → call write_file to CREATE the file immediately. Do NOT call read_file on the same path again.
- Never call read_file on the same path more than once. If not found: write it. If found: use the content.

STEP_DONE RULES:
- Call step_done the moment all acceptance criteria are satisfied — even at iteration 1.
- Do NOT keep editing after criteria are met. Do NOT verify by reading what you just wrote.
- The summary should list exactly what files were created/modified.
- After making all edits: use search() to check if stub/TODO patterns remain. If search returns 0 matches → call step_done IMMEDIATELY.
- Do NOT call read_file to verify your own edits. If edit_file returned success, trust it.

ANTI-LOOP RULES:
- If write_file succeeded → do NOT call write_file on the same path again (unless editing).
- If run_command succeeded → do NOT run the same command again.
- If read_file returned FILE NOT FOUND → do NOT call read_file on that path again. Write it.
- If an action returns the SAME error twice in a row → call need_help, do not retry.
- Track completed steps mentally. Move forward, not backward.
- NEVER call read_file on the same path more than ONCE per session. Use search() to find text within a file.
- After a successful edit_file: do NOT read the same file again. Proceed to the next edit or call step_done.
- If you have made all the required edits: call step_done. Do NOT spend iterations re-verifying.

TASK ID RULES:
- The task display_id (like "TASK-002") is shown in the task context. Use ONLY that exact ID.
- NEVER invent task IDs like "SUB-006", "TASK-123", "parent_task_id". Use the real ID from context.

CODE WRITING RULES:
- When writing Python code in write_file content, use SINGLE QUOTES for all strings inside the code.
  Example: pygame.display.set_caption('Pong')  NOT  pygame.display.set_caption("Pong")
- This prevents the double-quote characters from breaking the outer JSON string.
- Keep write_file content under 60 lines per call. Split large files into skeleton + edit_file additions.

PLACEHOLDER RULES (NEVER do these):
- NEVER use "/path/to/file.txt", "example.py", "New Task Title", "abc123" as values.
- ONLY use real values from the task description, file system, or DB query results.\
"""


# ---------------------------------------------------------------------------
# JSON extraction / repair helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """Try multiple strategies to extract a JSON object from raw AI output."""
    text = text.strip()

    # Strategy 1: entire text is valid JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: JSON inside a fenced code block  ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: first { … } substring (greedy from first { to last })
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        chunk = text[start : end + 1]
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 4: repair literal newlines/tabs inside JSON string values
        repaired = _repair_json_strings(chunk)
        if repaired != chunk:
            try:
                obj = json.loads(repaired)
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, ValueError):
                pass

        # Strategy 5: escape unescaped double quotes inside string values
        for _base in (chunk, repaired):
            _fixed = _surgical_quote_repair(_base)
            if _fixed != _base:
                try:
                    obj = json.loads(_fixed)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    pass

    # Strategy 6: truncated JSON — model ran out of tokens
    if start != -1:
        chunk6 = text[start:]
        completed = _complete_truncated_json(chunk6)
        if completed != chunk6:
            for candidate in (
                completed,
                _repair_json_strings(completed),
                _surgical_quote_repair(completed),
                _surgical_quote_repair(_repair_json_strings(completed)),
            ):
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    pass

    return None


def _complete_truncated_json(s: str) -> str:
    """Try to complete a truncated JSON object by closing open strings/brackets."""
    s = s.strip()
    in_string = False
    escape_next = False
    depth = 0

    for ch in s:
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1

    suffix = ""
    if in_string:
        suffix += '"'
    suffix += "}" * max(depth, 0)
    return s + suffix if suffix else s


def _repair_json_strings(s: str) -> str:
    """Escape literal newlines/tabs/carriage-returns inside JSON string values."""
    result: list = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\':
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _surgical_quote_repair(s: str) -> str:
    """Escape unescaped double quotes inside JSON string values (up to 30 rounds)."""
    BACKSLASH = chr(92)
    for _ in range(30):
        try:
            json.loads(s)
            return s
        except json.JSONDecodeError as e:
            found = False
            for i in range(e.pos - 1, -1, -1):
                if s[i] == '"':
                    bs_count = 0
                    j = i - 1
                    while j >= 0 and s[j] == BACKSLASH:
                        bs_count += 1
                        j -= 1
                    if bs_count % 2 == 0:
                        s = s[:i] + BACKSLASH + '"' + s[i + 1:]
                        found = True
                        break
            if not found:
                break
    return s


def _error_response(reason: str) -> dict:
    """Return a well-formed need_help action when something goes wrong."""
    return {
        "action": "need_help",
        "params": {"reason": reason},
        "reasoning": "Adapter error — could not obtain a valid response.",
    }


# ---------------------------------------------------------------------------
# Abstract base adapter
# ---------------------------------------------------------------------------

class BaseAdapter(ABC):
    """Abstract base for all provider adapters."""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def send(self, request: dict) -> dict:
        """Send a protocol request and return a protocol response dict."""

    def _build_prompt(self, request: dict) -> str:
        """Convert a protocol request dict into a plain-text prompt."""
        parts: List[str] = []

        # ── Existing project files ────────────────────────────────────────
        # Surfaced FIRST so the agent reckons with reality before reading the
        # task. Without this, agent_protocol set request["wiki_summary"] but
        # the prompt builder ignored it — every task prompted the model with
        # zero awareness of the codebase, and small models hallucinated
        # paths / created duplicates (game.py + src/game.py + game/game.py +
        # score_manager.py + game/scoring_system.py … all "new", none of
        # them edits of what existed).
        wiki = request.get("wiki_summary") or []
        if wiki:
            parts.append("== EXISTING PROJECT FILES ==")
            parts.append(
                "These files already exist. PREFER EDITING THEM over creating "
                "new files at new paths. Do NOT invent paths — every path you "
                "write/edit must either appear in this list or be an obvious "
                "new file the task description names explicitly."
            )
            for f in sorted(wiki, key=lambda x: x.get("path", "")):
                meta = []
                if f.get("language"):
                    meta.append(str(f["language"]))
                if f.get("line_count") is not None:
                    meta.append(f"{f['line_count']}L")
                if f.get("size_bytes") is not None:
                    meta.append(f"{f['size_bytes']}B")
                meta_str = "  [" + ", ".join(meta) + "]" if meta else ""
                parts.append(f"  {f.get('path', '?')}{meta_str}")
            parts.append("")

        task = request.get("task", {})
        if task:
            parts.append("== TASK ==")
            if task.get("title"):
                parts.append(f"Title: {task['title']}")
            if task.get("description"):
                parts.append(f"Description: {task['description']}")
            criteria = task.get("acceptance_criteria")
            if criteria:
                parts.append("Acceptance criteria:")
                if isinstance(criteria, list):
                    for c in criteria:
                        parts.append(f"  - {c}")
                else:
                    parts.append(f"  {criteria}")
            # Loop-control nudges set by AgentProtocol when the model is
            # close to its iteration budget or has already done useful work.
            # Without surfacing these in the prompt the model never sees the
            # "call step_done" hint and burns the whole step looping edit_file.
            if task.get("_completed"):
                parts.append(f"Completed: {task['_completed']}")
            if task.get("_budget_warning"):
                parts.append(f"!! BUDGET: {task['_budget_warning']}")
            elif task.get("_budget_note"):
                parts.append(f"Budget note: {task['_budget_note']}")

        task_ctx = request.get('task_context')
        if task_ctx:
            subtasks = task_ctx.get('subtasks', [])
            if subtasks:
                parts.append("\n== CHECKLIST ==")
                for st in subtasks:
                    check = 'x' if st.get('status') == 'done' else ' '
                    parts.append(f"  [{check}] {st.get('display_id', '')} {st.get('title', '')}")

            sprint = task_ctx.get('sprint_context')
            if sprint:
                parts.append("\n== SPRINT ==")
                parts.append(f"  Sprint {sprint.get('sprint_number', '?')}: {sprint.get('goal', '')}")
                planned = sprint.get('planned_points', 0) or sprint.get('velocity_planned', 0)
                completed = sprint.get('completed_points', 0) or sprint.get('velocity_actual', 0)
                parts.append(f"  Status: {sprint.get('sprint_status', '')} | Points: {completed}/{planned}")

            history_dots = task_ctx.get('recent_history', [])
            if history_dots:
                parts.append("\n== TASK HISTORY ==")
                for h in history_dots[-10:]:
                    detail = {}
                    try:
                        import json as _json
                        detail = _json.loads(h.get('detail', '{}')) if h.get('detail') else {}
                    except (ValueError, TypeError):
                        pass
                    msg = detail.get('message', '') or h.get('action', '')
                    parts.append(f"  - {h.get('timestamp', '')} [{h.get('actor', '')}] {msg}")

            linked = task_ctx.get('linked_files', [])
            if linked:
                parts.append("\n== LINKED FILES ==")
                for f in linked:
                    parts.append(f"  - {f.get('path', '')} ({f.get('relationship', '')})")

            file_contexts = task_ctx.get('file_contexts', [])
            if file_contexts:
                parts.append("\n== FILE SECTION DETAILS ==")
                for fc in file_contexts[:5]:
                    fp = fc.get('file_path', '?')
                    lock = fc.get('lock')
                    lock_str = f" [LOCKED by {lock['locked_by']}]" if lock else ""
                    parts.append(f"\n  {fp}{lock_str}")
                    for sh in fc.get('sections', [])[:10]:
                        s = sh.get('current', {})
                        pid = s.get('persistent_id', '')[:8] if s.get('persistent_id') else ''
                        parts.append(
                            f"    [{s.get('section_type','')}] {s.get('description','')} "
                            f"(GUID:{s.get('guid','')[:12]}... PID:{pid}... rev {s.get('revision',1)})"
                        )
                        for rev in sh.get('recent_revisions', []):
                            parts.append(
                                f"      rev {rev.get('revision')}: \"{rev.get('description','')[:60]}\" "
                                f"by {rev.get('created_by','')} at {rev.get('created_at','')[:16]}"
                            )

            modifications = task_ctx.get('file_modifications', [])
            if modifications:
                parts.append("\n== FILE MODIFICATIONS ==")
                for m in modifications[-10:]:
                    parts.append(f"  - {m.get('timestamp', '?')} {m.get('action', '?')}: {m.get('path', '?')}")

            parent_chain = task_ctx.get('parent_chain', [])
            if parent_chain:
                chain_ids = [p.get('display_id', '?') for p in parent_chain]
                task_id = task_ctx.get('task', {}).get('display_id', '?')
                chain_ids.append(task_id)
                parts.append("\n== TASK HIERARCHY ==")
                parts.append(f"  {' > '.join(chain_ids)}")

            children = task_ctx.get('children', [])
            if children:
                parts.append(f"\n== CHILD TASKS ({len(children)}) ==")
                for c in children:
                    parts.append(
                        f"  {c.get('display_id', '?')}: {c.get('title', '')} [{c.get('status', '?')}]"
                    )

        ctx = request.get("context", {})
        if ctx:
            parts.append("\n== CONTEXT ==")
            for key in ("project_dir", "build_status", "test_status"):
                if ctx.get(key):
                    parts.append(f"{key}: {ctx[key]}")
            if ctx.get("files"):
                parts.append("Relevant files:")
                for f in ctx["files"][:20]:
                    parts.append(f"  {f}")

            audit = ctx.get("audit")
            if audit:
                parts.append("\n== SPRINT AUDIT DATA ==")
                parts.append(f"  Files modified: {len(audit.get('files_modified', []))}")
                for f in audit.get('files_modified', [])[:20]:
                    parts.append(f"    - {f}")
                parts.append(f"  Prompt exchanges: {audit.get('prompt_count', 0)}")
                parts.append(f"  Errors: {audit.get('error_count', 0)}")
                parts.append(f"  Tasks completed: {audit.get('completed', 0)}")
                parts.append(f"  Tasks failed: {audit.get('failed', 0)}")
                counts = audit.get('action_counts', {})
                if counts:
                    parts.append("  Action breakdown:")
                    for atype, cnt in sorted(counts.items(), key=lambda x: -x[1])[:15]:
                        parts.append(f"    {atype}: {cnt}")
                errors = audit.get('error_patterns', [])
                if errors:
                    parts.append("  Error patterns:")
                    for e in errors[:5]:
                        parts.append(f"    [{e.get('action','')}] {e.get('detail','')[:100]}")

        instructions = request.get("instructions", {})
        if instructions:
            parts.append("\n== INSTRUCTIONS ==")
            if instructions.get("step_prompt"):
                parts.append(f"Step instructions:\n{instructions['step_prompt']}")
            if instructions.get("agent_definition"):
                parts.append(f"\nAgent role:\n{instructions['agent_definition']}")
            standards = instructions.get("standards")
            if standards:
                parts.append("\nProject standards:")
                if isinstance(standards, list):
                    for s in standards:
                        if isinstance(s, dict):
                            parts.append(f"\n--- {s.get('name', 'Standard')} ---\n{s.get('content', '')}")
                        else:
                            parts.append(str(s))
                else:
                    parts.append(str(standards))
            if instructions.get("expert"):
                exp = instructions["expert"]
                if isinstance(exp, dict):
                    parts.append(f"\nLanguage expert ({exp.get('language','')}):\n{exp.get('content','')}")
                else:
                    parts.append(f"\nExpert:\n{exp}")

        history = request.get("history", [])
        if history:
            recent = history[-10:]
            parts.append("\n== RECENT HISTORY ==")
            for entry in recent:
                if isinstance(entry, dict):
                    role = entry.get("role", "?")
                    content = str(entry.get("content", ""))[:500]
                    parts.append(f"[{role}] {content}")
                else:
                    parts.append(f"- {str(entry)[:300]}")

        actions = request.get("available_actions")
        if actions:
            try:
                from validation import get_action_documentation
                parts.append('\n' + get_action_documentation())
            except ImportError:
                parts.append("\n== AVAILABLE ACTIONS ==")
            for a in actions:
                if isinstance(a, dict):
                    name = a.get('name', '?')
                    desc = a.get('description', '')
                    params = a.get('params', {})
                    param_str = (', '.join(f'{k}={v}' for k, v in params.items())
                                 if isinstance(params, dict) else str(params))
                    parts.append(f"  - {name}: {desc} ({param_str})")
                else:
                    parts.append(f"  - {a}")

        perms = request.get("permissions", {})
        if perms:
            parts.append("\n== PERMISSIONS ==")
            for k, v in perms.items():
                parts.append(f"  {k}: {v}")

        return "\n".join(parts)

    def _parse_response(self, text: str) -> dict:
        """Extract a JSON action dict from raw AI output."""
        if not text or not text.strip():
            return _error_response("Empty response from AI provider.")

        obj = _extract_json(text)
        if obj is None:
            return _error_response(
                f"Could not parse JSON from response (first 500 chars): {text[:500]}"
            )

        if "action" not in obj:
            return _error_response(
                f"Response JSON missing 'action' key: {json.dumps(obj)[:500]}"
            )
        if obj["action"] not in VALID_ACTIONS:
            return _error_response(
                f"Unknown action '{obj['action']}'. Valid: {', '.join(sorted(VALID_ACTIONS))}"
            )
        obj.setdefault("params", {})
        obj.setdefault("reasoning", "")
        return obj

    def _is_parse_error(self, response: dict) -> bool:
        """True if the response is an error caused by JSON parse failure."""
        if response.get('action') != 'need_help':
            return False
        reason = response.get('params', {}).get('reason', '')
        return ('parse' in reason.lower() or 'json' in reason.lower() or
                'missing' in reason.lower() or 'unknown action' in reason.lower())
    
    def generate(self, request: dict) -> str:
        """Simple text generation without full protocol overhead.
        
        Default implementation uses send() as fallback. Subclasses should override
        with a more efficient direct generation method if available.
        
        Args:
            request: dict with 'prompt', optional 'max_tokens', 'temperature'
        
        Returns:
            Raw text response from the model
        """
        # Fallback: construct minimal protocol request and extract response
        mock_request = {
            "protocol": "automatica-agent/1.0",
            "task": {"description": request.get("prompt", "")},
            "context": {},
            "instructions": {"role": "You are a planning assistant. Return only valid JSON."},
            "available_actions": [],
            "permissions": {},
        }
        try:
            response = self.send(mock_request)
            return response.get("reasoning", "") or str(response)
        except Exception as exc:
            return f"Error: {exc}"
