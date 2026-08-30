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
    # Merge: consolidate duplicate files into a canonical one
    'merge_files',
    # Wiki / search / summary helpers exposed to agents
    'query_wiki', 'find_relevant_files', 'get_summary', 'take_note',
    'search_by_guid', 'get_json_keys',
    # File ops
    'copy_file',
    # Subtask actions (also exposed to executor)
    'create_subtask', 'complete_subtask',
    # Handoff payload between iterations
    'store_handoff',
])


# ---------------------------------------------------------------------------
# Universal system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an AI software agent operating via a structured JSON protocol.
You MUST respond with ONLY a single JSON object. No markdown, no code blocks, no explanation outside the JSON.

Two response shapes are supported. Pick whichever matches what you need to do this turn.

A) SINGLE-ACTION (one operation this turn):
{"action": "action_name", "params": {...}, "reasoning": "brief reason"}

B) MULTI-ACTION BATCH (preferred when several related operations belong together —
   e.g. create three test files, or write a class + register it + add a test):
{"actions": [
   {"action": "write_file", "params": {"path": "src/foo.py", "content": "..."}},
   {"action": "write_file", "params": {"path": "tests/test_foo.py", "content": "..."}},
   {"action": "step_done",  "params": {"summary": "Created foo + its test"}}
 ],
 "reasoning": "Both files needed for this step; create together."}

Batch rules:
  - Actions execute in order. If one fails, the remaining are skipped and you
    get the failure result back next turn.
  - You can include step_done as the LAST entry to finish the step in the same
    round-trip after your edits succeed.
  - You can include need_help mid-batch to abort early with a reason.
  - Do NOT mix exploration (read_file / list_files / search) into the same
    batch as edits — read first, decide, then batch the edits in the next turn.
  - 5 actions per batch is a sensible upper bound; bigger batches risk one
    failure invalidating a lot of related work.

Choose exactly one action (or one batch) from the available_actions list.

Core actions:
- "read_file":  Read a file. Params: {"path": "relative/path.py"}
- "write_file": Create / overwrite a file. Always pass overwrite:true. Params: {"path": "...", "content": "...", "overwrite": true}
- "edit_file":  Replace EXACT text. Params: {"path": "...", "old_text": "...", "new_text": "..."}
- "copy_file":  Duplicate a file inside the project. Params: {"src": "...", "dst": "...", "overwrite": false}
- "merge_files": Consolidate a duplicate into the canonical file. The system reads both, dedupes imports, appends non-duplicate definitions from source into target, and discards the source. Use this when write_file rejects with a basename collision and you want option (1) MERGE done for you. Params: {"target": "src/canonical.py", "source": "src/dupe.py" OR "new_content": "..."}
- "discard_file": SOFT-delete (moves to _Discarded/, reversible). Use this instead of `rm`. Params: {"file_path": "...", "reason": "..."}
- "restore_file": Bring back a discarded file. Params: {"discard_id": ...} OR {"original_path": "..."}
- "list_files": List files. Params: {"pattern": "src/**/*.py"}
- "search":     Regex grep across files. Params: {"pattern": "...", "glob": "*.py"}
- "find_relevant_files": Rank files by relevance to a free-text query. Params: {"query": "...", "top_k": 10}
- "get_summary": Read just the docstring/folder summary of a path (cheaper than read_file). Params: {"path": "..."}
- "run_command": Run an allowed shell command. Chains with && or ; are split into separate subprocess calls. Params: {"command": "..."}
- "take_note":  Write to your persistent per-task scratchpad. Auto-included in every iteration's prompt for THIS task. Params: {"text": "...", "mode": "append"|"replace"}
- "screenshot" / "screenshot_window" / "analyze_screenshot": Visual inspection of a running app. See available_actions for params.
- "step_done":  Task complete. Call this ONCE all criteria are met. Params: {"summary": "what was done"}
- "need_help":  Stuck or blocked. Params: {"reason": "why stuck"}

NEVER reach into .git, .automatica, venv, .vscode, node_modules, __pycache__,
or any test cache directory — every path you read or write must be a real
project source path, and the executor will reject system-directory paths.

ANTI-DUPLICATE RULE (HARD):
If the task title or description starts with REFACTOR, EXTRACT, RENAME,
SPLIT, MOVE, RESTRUCTURE, INLINE, MERGE, OPTIMISE, POLISH, ADD DOCSTRINGS,
or otherwise targets EXISTING code, you MUST modify in place:
  - Use edit_file (or write_file with overwrite:true on the SAME path)
  - Use discard_file + write_file ONLY when the task names a new path
    explicitly (e.g. "create src/ai/paddle.py with ...")
  - Multiple edit_file calls in one batch are encouraged (see batch shape)

DO NOT:
  - Create paddle/movement.py when src/paddle.py already exists
  - Create paddle_ai/foo.py because the original lived at src/paddle.py
  - Make a sibling file in a new folder "to keep the original safe"
  - Invent files like PaddleTracking.py or ScoringSystem.py if the task
    only said "refactor scoring logic"
  - Treat "extract X" as "create new file" — extract means split logic
    inside the SAME file (separate class) unless a new path is named

If you produce a path that doesn't appear in the EXISTING PROJECT FILES
list AND isn't named by the task description, the executor will warn you.
Treat that warning as a near-failure — the next action should fix the
duplicate (discard_file the new one, edit the existing).

WHEN write_file IS REJECTED FOR BASENAME COLLISION:
The rejection message echoes (1) the existing file's content and (2)
your submitted content. Your DEFAULT response is one of these — DO NOT
invent a third path:

  Option 1 (preferred, automatic):
    {"action":"merge_files","params":{"target":"<existing path>",
     "source":"<your rejected path>"}}
    OR (if you only have content, no source file yet):
    {"action":"merge_files","params":{"target":"<existing path>",
     "new_content":"<the content you tried to write>"}}
    The system AST-merges unique imports/defs/classes, dedupes overlap,
    and discards the source. One call, done.

  Option 2 (replace existing entirely):
    {"action":"write_file","params":{"path":"<existing path>",
     "content":"<your content>","overwrite":true}}

  Option 3 (genuinely separate concerns — only if the existing file is
  unrelated): first call read_file on the existing path to inspect it,
  then re-issue write_file with acknowledge_duplicate=true and a
  reason="<why these are different concerns>".

NEVER respond to a basename-collision rejection by writing the same
content to a third invented path. NEVER invent files like
PaddleAI/Movement.py just because you can't write paddle.py — call
merge_files instead.

CRITICAL WORKFLOW — follow exactly:
1. Read the task description carefully.
2. EXPLORE FIRST: call list_files or read_file to understand the EXISTING codebase before making any changes.
   - NEVER invent file paths. Only use paths you have confirmed exist via list_files or read_file.
   - For modification tasks: read the target file first, then use edit_file (not write_file) to change it.
3. Perform each required action ONE AT A TIME (edit file, run command, etc.).
4. After each SUCCESS, mentally tick off that criterion. Do NOT redo completed steps.
5. Once ALL acceptance criteria are satisfied → call step_done IMMEDIATELY. Do not do extra work.
6. If an action fails TWICE with the same error → call need_help, do not retry endlessly.

EXPLORE-BEFORE-MODIFY RULES (apply to existing codebases — NOT empty projects):
- For tasks against an EXISTING file, read it first with read_file, then edit_file.
- For SETUP / CREATE / SCAFFOLD tasks (project structure, new modules, missing
  files): write_file directly. DO NOT read non-existent files first — that
  burns iterations. The EXISTING PROJECT FILES list in the prompt is your
  canonical source of what's already on disk; treat anything not in that
  list as "doesn't exist; write it" without verifying.
- HARD CAP: never spend more than 2 iterations on read_file/list_files/search
  combined before doing real work. Iteration 3 onward must be
  write_file/edit_file/run_command or step_done.
- If read_file returns FILE NOT FOUND → call write_file to CREATE the file
  immediately. Do NOT call read_file on the same path again.
- Prefer edit_file over write_file for changes to existing files.

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
- ONLY use real values from the task description, file system, or DB query results.

TESTING & VERIFICATION (you CAN run code — use this):
- run_command lets you execute python, pytest, py_compile, and other shell tools.
  Use it to VERIFY your work, not just to "look productive."
- After writing or editing source code, ALWAYS verify with at least one of:
    • run_command "python -m py_compile <file>"   (syntax check, fast)
    • run_command "python -c \\"import <module>\\""  (import smoke test)
    • run_command "python -m pytest <test_file> -x -q"  (if a test exists)
- For headless GUI/game code (pygame, kivy, tkinter): set the dummy driver
  before importing — run_command "python -c \\"import os; os.environ['SDL_VIDEODRIVER']='dummy'; import pong; ...\\""
- If a task creates a class with mutable state (Ball, Paddle, Player), write a
  SHORT smoke test in tests/ that constructs it, calls its update method N
  times, and asserts invariants (e.g. position stays in bounds).
- A failing smoke test is a SUCCESS signal: you found a bug. Either fix it in
  the same step (extra edit_file) or take_note describing the bug and call
  step_done — the next sprint can pick it up.
- Never claim step_done on a task that wrote new code without at least one
  py_compile or import check, unless the task is purely documentation.\
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

        # ── Project standards (CLAUDE.md or equivalent) ───────────────────
        # The user-defined "what good looks like in this project" doc.
        # Surfaced before the task block so SOLID rules, project layout,
        # build/test gates, and language-specific requirements (KivyMD for
        # Python UI, build.bat, YAML configs, ≥90% coverage) anchor every
        # agent action.
        std = request.get("project_standards")
        if std and std.get("text"):
            parts.append(f"== PROJECT STANDARDS (from {std.get('source','CLAUDE.md')}) ==")
            parts.append(std["text"].rstrip())
            parts.append(("Conform every change to these standards. If the "
                          "task contradicts a standard, prefer the standard "
                          "and call need_help."))
            parts.append("")

        # ── Project context / "why" notes (set during interactive setup) ──
        ctx = request.get("project_context")
        if ctx and ctx.strip():
            parts.append("== PROJECT CONTEXT (why this matters / constraints) ==")
            parts.append(ctx.rstrip())
            parts.append("")

        # ── Existing project files ────────────────────────────────────────
        # Surfaced FIRST (well — second, after standards) so the agent reckons
        # with reality before reading the task. Without this, agent_protocol
        # set request["wiki_summary"] but the prompt builder ignored it —
        # every task prompted the model with zero awareness of the codebase,
        # and small models hallucinated paths / created duplicates (game.py +
        # src/game.py + game/game.py + score_manager.py + game/scoring_system.py
        # … all "new", none of them edits of what existed).
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

        # Auto-injected persistent notepad for this task. Written by the
        # take_note action; read every iteration so the agent doesn't
        # re-derive its own plan from scratch each time.
        notes = request.get("agent_notes")
        if notes:
            parts.append("")
            parts.append("== YOUR PERSISTENT NOTES (from take_note) ==")
            parts.append(notes.rstrip())
            parts.append("(Use take_note to add or replace this scratchpad. "
                          "It's auto-included in every iteration's prompt.)")

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

        # ── Bottom-of-prompt schema anchor ────────────────────────────────────
        # Small open-weight models (deepseek-coder 6.7B, mistral 7B, phi4 14B)
        # lose track of format constraints buried in a 10k-token system prompt.
        # Repeating the JSON schema right before the response slot keeps them
        # anchored on the action shape instead of emitting {} or echoing the
        # planning-quality-gate's {is_complete, missing} shape from earlier turns.
        parts.append(
            "\n== RESPOND NOW ==\n"
            "Output ONE JSON object only. No prose, no code fences, no markdown.\n"
            "Pick ONE shape:\n"
            "  Single:  {\"action\":\"<name>\",\"params\":{...},\"reasoning\":\"...\"}\n"
            "  Batch:   {\"actions\":[{\"action\":\"<name>\",\"params\":{...}},...],"
            "\"reasoning\":\"...\"}\n"
            "<name> MUST be from the available_actions list above. "
            "Do NOT emit {} or {\"is_complete\":...}.\n"
            "If unsure what to do next, call \"list_files\" or \"read_file\" — "
            "never empty JSON."
        )

        return "\n".join(parts)

    def _parse_response(self, text: str) -> dict:
        """Extract a JSON action dict from raw AI output.

        Two shapes accepted:

        Single-action (legacy)::

            {"action": "edit_file", "params": {...}, "reasoning": "..."}

        Multi-action batch (preferred for related ops in one round-trip)::

            {"actions": [
                {"action": "write_file", "params": {...}},
                {"action": "edit_file",  "params": {...}},
                {"action": "step_done",  "params": {"summary": "..."}}
             ],
             "reasoning": "..."}

        Multi-action is normalised to a single dict with an ``actions`` list
        plus a synthetic top-level ``action`` set to the FIRST entry — that
        keeps every existing caller happy (they read ``response['action']``)
        while ``response['actions']`` lets the loop fan out the batch.
        """
        if not text or not text.strip():
            return _error_response("Empty response from AI provider.")

        obj = _extract_json(text)
        if obj is None:
            return _error_response(
                f"Could not parse JSON from response (first 500 chars): {text[:500]}"
            )

        # Multi-action batch shape ────────────────────────────────────────
        if "actions" in obj and isinstance(obj["actions"], list) and obj["actions"]:
            cleaned: list[dict] = []
            for i, sub in enumerate(obj["actions"]):
                if not isinstance(sub, dict):
                    return _error_response(
                        f"actions[{i}] is not an object: {sub!r}"
                    )
                if "action" not in sub:
                    return _error_response(
                        f"actions[{i}] missing 'action' key: {json.dumps(sub)[:200]}"
                    )
                if sub["action"] not in VALID_ACTIONS:
                    return _error_response(
                        f"actions[{i}] unknown action {sub['action']!r}. "
                        f"Valid: {', '.join(sorted(VALID_ACTIONS))}"
                    )
                sub.setdefault("params", {})
                cleaned.append(sub)
            obj["actions"] = cleaned
            # Synthesise top-level action/params from the first entry so the
            # legacy "response['action']" code path keeps working.
            obj.setdefault("action", cleaned[0]["action"])
            obj.setdefault("params", cleaned[0].get("params", {}))
            obj.setdefault("reasoning", "")
            return obj

        # Single-action shape (legacy) ────────────────────────────────────
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
        # Fallback: construct minimal protocol request and extract response.
        # generate() is used for planning/scratch calls — by definition no
        # files should be edited. Set expects_changes=False so the agentic
        # adapter's WF-8 zero-diff classifier does NOT mark the call as a
        # false-success block. Caller may override by passing the flag.
        mock_request = {
            "protocol": "automatica-agent/1.0",
            "task": {"description": request.get("prompt", "")},
            "context": {},
            "instructions": {"role": "You are a planning assistant. Return only valid JSON."},
            "available_actions": [],
            "permissions": {},
            "expects_changes": bool(request.get("expects_changes", False)),
        }
        try:
            response = self.send(mock_request)
            return response.get("reasoning", "") or str(response)
        except Exception as exc:
            return f"Error: {exc}"
