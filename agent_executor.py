#!/usr/bin/env python3
"""
AIxYad Agent Executor — Sandboxed action execution for external AI agents.

Executes file operations, searches, and commands within a permission sandbox.
Every action is checked against permissions before execution.
Results include full file contents (for reads/searches) so the AI gets context.
"""

import contextlib
import fnmatch
import json
import os
import re
import shlex
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aixyad_paths


# OS-level file mutex (cross-process, cross-thread) to add a hard layer on
# top of the advisory file_locks table. Optional dependency: degrades to
# a no-op if portalocker is missing.
try:
    import portalocker as _portalocker
except ImportError:
    _portalocker = None


@contextlib.contextmanager
def _os_file_lock(target_path: Path, timeout: float = 30.0):
    """Acquire an exclusive OS-level lock on a sidecar file, then release.
    Uses portalocker when available; falls back to a no-op if the optional
    dependency isn't installed (so this module stays importable everywhere).

    The lock targets <target_path>.lock not the file itself, so concurrent
    readers of the data file are unaffected and the lock works even when the
    target file doesn't exist yet (write_file create case).
    """
    if _portalocker is None:
        yield
        return
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # portalocker v3 dropped the timeout kwarg from .lock() in favour of the
    # Lock context manager. Loop with a short retry instead.
    import time as _time
    f = open(lock_path, "a+")
    deadline = _time.monotonic() + max(0.1, timeout)
    acquired = False
    try:
        while _time.monotonic() < deadline:
            try:
                _portalocker.lock(f, _portalocker.LOCK_EX | _portalocker.LOCK_NB)
                acquired = True
                break
            except (_portalocker.exceptions.LockException, OSError):
                _time.sleep(0.05)
        if not acquired:
            # Last attempt: blocking lock (no NB flag) — will wait until OS releases
            _portalocker.lock(f, _portalocker.LOCK_EX)
            acquired = True
        yield
    finally:
        if acquired:
            try:
                _portalocker.unlock(f)
            except Exception:
                pass
        f.close()
        # Remove the empty sidecar so it doesn't pollute the working tree.
        # The .lock file is purely a portalocker handle target — once unlocked
        # and the file handle is closed, we no longer need it on disk. Without
        # this every write_file/edit_file leaves a ".lock" file behind which
        # then shows up in git status, gets committed by accident, etc.
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass  # best-effort; ok if another holder is racing


try:
    from vision_actions import VisionActionHandler as _VisionActionHandler
    _VISION_AVAILABLE = True
except ImportError:
    _VisionActionHandler = None  # type: ignore
    _VISION_AVAILABLE = False

try:
    from view_actions import ViewActionHandler as _ViewActionHandler
    _VIEW_AVAILABLE = True
except ImportError:
    _ViewActionHandler = None  # type: ignore
    _VIEW_AVAILABLE = False

# Vision action names for routing
_VISION_ACTIONS = frozenset({
    'screenshot', 'screenshot_window', 'screenshot_region',
    'analyze_image', 'analyze_screenshot',
    'ui_click', 'ui_type', 'ui_key_press', 'ui_scroll', 'ui_drag', 'ui_type_in_field',
    'launch_app', 'get_window_info', 'list_windows', 'bring_window_to_front',
    'get_screen_size', 'get_mouse_position',
})

# View action names for routing
_VIEW_ACTIONS = frozenset({
    'get_view', 'set_view', 'get_view_compact',
    'get_related_content', 'get_related_keywords', 'set_related_keywords',
    'replace_section', 'insert_line', 'remove_section', 'rename_symbol',
})


class AgentExecutor:
    """Execute agent actions with permission checking and sandboxing."""

    # Commands considered safe (prefix-matched)
    SAFE_COMMAND_PREFIXES = [
        "cmake", "ctest", "git status", "git diff", "git log",
        "git add", "git commit", "git branch", "git checkout", "git stash",
        "git tag", "git fetch", "git remote", "git show", "git blame",
        "python", "pip", "npm", "node",
        "cargo", "rustc", "go ", "dotnet", "make", "ninja",
        # Windows shell - read-only and verification commands allowed
        "powershell -command", "powershell -noprofile -command",
        "cmd /c type", "cmd /c dir", "cmd /c echo", "cmd /c findstr",
        "where ", "type ",
    ]

    # VCS commands that need explicit permission (dangerous by default)
    VCS_PERMISSION_MAP = {
        'git push':         'can_git_push',
        'git reset --hard': 'can_git_reset',
        'git reset':        'can_git_reset',
        'git clean':        'can_git_clean',
        'git rebase':       'can_git_rebase',
        'git merge':        'can_git_merge',
        'git branch -D':    'can_git_delete_branch',
        'git branch -d':    'can_git_delete_branch',
        'git checkout --':  'can_git_discard',
        'git restore':      'can_git_discard',
    }
    VCS_PERMISSION_DEFAULTS = {
        'can_git_push': False,
        'can_git_reset': False,
        'can_git_clean': False,
        'can_git_rebase': False,
        'can_git_merge': True,
        'can_git_delete_branch': False,
        'can_git_discard': False,
    }

    # Actions that always bypass permission checks
    ALWAYS_ALLOWED = {"step_done", "need_help"}

    # Map actions to their permission keys
    PERMISSION_MAP = {
        "read_file": "can_read_file",
        "write_file": "can_write_file",
        "edit_file": "can_edit_file",
        "search": "can_search",
        "list_files": "can_search",
        "run_command": "can_run_command",
        "run_script": "can_run_command",
        "commit": "can_commit",
        # Scanner actions (read-only)
        "query_file_structure": "can_search",
        "query_symbols": "can_search",
        "query_files": "can_search",
        "query_file_section": "can_search",
        "query_wiki": "can_search",
        "search_by_guid": "can_search",
        "get_json_keys": "can_search",  # S3-W02
        # DB query actions (read-only)
        "query_tasks": "can_search",
        "query_task_detail": "can_search",
        "query_sprint": "can_search",
        "query_sprint_summary": "can_search",
        "query_history": "can_search",
        "query_task_timeline": "can_search",
        # File ops added 2026-05-06: copy + soft-delete-with-backup + notes
        "copy_file":              "can_write_file",
        "discard_file":           "can_write_file",  # soft delete to _Discarded/
        "restore_file":           "can_write_file",
        "take_note":              "can_write_file",  # writes to .aixyad/notes/
        # DB mutation actions (write)
        "update_task": "can_write_file",
        "add_task": "can_write_file",
        "reorder_tasks": "can_write_file",
        "add_checklist_item": "can_write_file",
        "check_item": "can_write_file",
        "link_file_to_task": "can_write_file",
        "log_file_modification": "can_write_file",
        "create_subtask": "can_write_file",
        "complete_subtask": "can_write_file",
        "store_handoff": "can_write_file",
        # Vision / UI automation actions
        "screenshot":           "can_screenshot",
        "screenshot_window":    "can_screenshot",
        "screenshot_region":    "can_screenshot",
        "analyze_image":        "can_screenshot",
        "analyze_screenshot":   "can_screenshot",
        "ui_click":             "can_ui_input",
        "ui_type":              "can_ui_input",
        "ui_key_press":         "can_ui_input",
        "ui_scroll":            "can_ui_input",
        "ui_drag":              "can_ui_input",
        "ui_type_in_field":     "can_ui_input",
        "launch_app":           "can_run_command",
        "get_window_info":      "can_screenshot",
        "list_windows":         "can_screenshot",
        "bring_window_to_front":"can_screenshot",
        "get_screen_size":      "can_screenshot",
        "get_mouse_position":   "can_screenshot",
        # Config/agent definition safe accessors (read-only from project DB)
        "get_config":           "can_search",
        "set_config":           "can_write_file",
        "get_agent_def":        "can_search",
        # View / file-tree / structured-edit actions
        "get_view":             "can_search",
        "set_view":             "can_search",
        "get_view_compact":     "can_search",
        "get_related_content":  "can_search",
        "get_related_keywords": "can_search",
        "set_related_keywords": "can_search",
        "replace_section":      "can_edit_file",
        "insert_line":          "can_edit_file",
        "remove_section":       "can_edit_file",
        "rename_symbol":        "can_edit_file",
    }

    # Per-language file-extension allowlists. write_file rejects writes whose
    # extension isn't in the allowlist for the project's detected language.
    # Catches cross-language sprawl like main.cpp landing in a Python project.
    LANGUAGE_EXTENSIONS = {
        "python": {".py", ".pyx", ".pyi", ".md", ".rst", ".txt", ".yaml", ".yml",
                   ".json", ".toml", ".cfg", ".ini", ".bat", ".sh",
                   ".html", ".css", ".js"},  # html/css/js OK for templates/static
        "cpp": {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx",
                ".cmake", ".md", ".txt", ".yaml", ".yml", ".json"},
        "csharp": {".cs", ".csproj", ".sln", ".xaml", ".cshtml",
                   ".md", ".txt", ".yaml", ".yml", ".json"},
        "go": {".go", ".mod", ".sum", ".md", ".txt", ".yaml", ".yml", ".json"},
        "rust": {".rs", ".toml", ".md", ".txt", ".yaml", ".yml", ".json"},
        "java": {".java", ".kt", ".groovy", ".gradle", ".xml",
                 ".md", ".txt", ".yaml", ".yml", ".json"},
        "typescript": {".ts", ".tsx", ".js", ".jsx", ".json", ".html", ".css",
                       ".md", ".txt", ".yaml", ".yml"},
    }

    # Files whose basename matches these patterns are scratchpad/temp files
    # the agent should NOT be writing — they accumulate sprawl. take_note
    # is the right place for scratch thinking.
    TEMP_FILE_PATTERNS = (
        re.compile(r"^_?temp[._].*$", re.IGNORECASE),
        re.compile(r"^temp_.*$", re.IGNORECASE),
        re.compile(r"^_temp_.*$", re.IGNORECASE),
        re.compile(r"^scratch[._].*$", re.IGNORECASE),
        re.compile(r"^scratch_.*$", re.IGNORECASE),
        re.compile(r"^foo[._].*$", re.IGNORECASE),
        re.compile(r"^bar[._].*$", re.IGNORECASE),
        re.compile(r"^.*\.bak$", re.IGNORECASE),
        re.compile(r"^.*\.orig$", re.IGNORECASE),
    )

    # Directories whose names indicate temp/scratch — entire subtree is banned.
    TEMP_DIR_NAMES = frozenset({"_temp", "temp", "scratch", "tmp", "_tmp"})

    def __init__(self, project_dir: Path, permissions: Dict[str, Any],
                 action_filter: Optional[List[str]] = None,
                 agent_slug: str = '',
                 language: str = ''):
        self.project_dir = Path(project_dir).resolve()
        self.permissions = permissions
        self._language = (language or '').lower().strip()
        # E-16: optional whitelist of allowed action names for this agent
        self._action_filter: Optional[frozenset] = (
            frozenset(action_filter) if action_filter else None
        )
        self._agent_slug = agent_slug
        self.action_log: List[Dict] = []
        self._db_handler = None
        self._session_actor = ''
        self._session_id = ''
        self._vision_handler = _VisionActionHandler() if _VISION_AVAILABLE else None
        self._view_handler = None  # set via set_view_handler()
        # Set by AgentProtocol after construction. Files touched by write/edit
        # land in this queue and the SprintRunner drains it at sprint end —
        # change-driven docstring/folder/README summary refresh.
        self._summary_indexer = None  # type: ignore[assignment]
        # Display ID of the task currently running. Used by take_note to
        # route entries to the right notepad and by _build_prompt to inject
        # the task's persistent notes into every iteration.
        self._current_task_id: str = ""
        # Files the agent has actually inspected this session (via read_file,
        # merge_files, or write_file overwrite). Used by the basename-collision
        # gate: agent can only "keep separate" via acknowledge_duplicate=True
        # AFTER they have actually looked at the existing canonical file.
        self._inspected_paths: set[str] = set()

    def set_summary_indexer(self, indexer) -> None:
        """Attach a SummaryIndexer so file-mutating actions auto-enqueue."""
        self._summary_indexer = indexer

    def set_current_task_id(self, task_id: str) -> None:
        """Tell the executor which task is currently running, so take_note
        and the auto-injected notepad land on the right per-task file."""
        self._current_task_id = (task_id or "").strip()

    def set_db_handler(self, handler, actor: str = '', session_id: str = ''):
        """Attach a DbActionHandler for DB action routing and action logging."""
        self._db_handler = handler
        self._session_actor = actor
        self._session_id = session_id

    def set_view_handler(self, handler):
        """Attach a ViewActionHandler for hierarchical file-tree and structured-edit actions."""
        self._view_handler = handler

    def execute(self, response: dict) -> dict:
        """Execute an action from AI response. Returns result dict with 'success' and 'message'."""
        action = response.get("action", "")
        params = response.get("params", {})
        reasoning = response.get("reasoning", "")
        timestamp = datetime.now().isoformat()

        # Always-allowed actions pass through without checks
        if action in self.ALWAYS_ALLOWED:
            result = {"success": True, "message": f"ACTION: {action} — ACKNOWLEDGED"}
            self._log(timestamp, action, params, result, reasoning)
            return result

        # E-16: agent action filter — reject actions not in agent's allowed list
        if self._action_filter is not None and action not in self._action_filter:
            result = {
                "success": False,
                "message": (
                    f"ACTION: {action!r} not permitted for agent {self._agent_slug!r}. "
                    f"Allowed: {sorted(self._action_filter)}"
                ),
            }
            self._log(timestamp, action, params, result, reasoning)
            return result

        # Check permission
        perm_key = self.PERMISSION_MAP.get(action)
        if perm_key and not self.permissions.get(perm_key, True):
            result = {
                "success": False,
                "message": f"ACTION: {action} — DENIED (permission '{perm_key}' is false)",
            }
            self._log(timestamp, action, params, result, reasoning)
            return result

        # Path safety check for file operations
        if "path" in params:
            path_check = self._check_path(params["path"])
            if not path_check["safe"]:
                result = {
                    "success": False,
                    "message": f"ACTION: {action} — DENIED ({path_check['reason']})",
                }
                self._log(timestamp, action, params, result, reasoning)
                return result

        # Route DB actions to DbActionHandler if available
        _DB_ACTIONS = frozenset({
            'query_tasks', 'query_task_detail', 'update_task', 'add_task',
            'reorder_tasks', 'add_checklist_item', 'check_item',
            'query_sprint', 'query_sprint_summary', 'query_history',
            'query_task_timeline', 'link_file_to_task', 'log_file_modification',
            # GUID code section tracking
            'create_section', 'edit_section', 'find_section', 'get_section_history',
            # Reservation and assignment
            'reserve_task', 'release_task', 'assign_task', 'reserve_subtask',
            # Safe delete / discard
            'discard_task', 'restore_task', 'discard_file', 'restore_file',
            'query_discarded_files', 'query_file_history',
            # File locking
            'lock_file', 'unlock_file', 'check_file_lock',
            # Prompt logs
            'query_prompt_logs',
            # Config and agent-def access
            'get_config', 'set_config', 'get_agent_def',
            # Subtask management (complete_subtask and store_handoff handled by DbActionHandler)
            'complete_subtask', 'store_handoff',
        })
        if action in _DB_ACTIONS:
            if not self._db_handler:
                result = {"success": False, "message": f"ACTION: {action} — NO DB CONNECTION (project_db not set)"}
                self._log(timestamp, action, params, result, reasoning)
                return result
            try:
                result = self._db_handler.handle(
                    action, params,
                    actor=self._session_actor, session_id=self._session_id,
                )
            except Exception as exc:
                result = {"success": False, "message": f"ACTION: {action} — DB ERROR: {exc}"}
            self._log(timestamp, action, params, result, reasoning)
            return result

        # Route vision/UI actions to VisionActionHandler
        if action in _VISION_ACTIONS:
            if not self._vision_handler:
                result = {"success": False, "message": f"ACTION: {action} — vision_actions module not available"}
                self._log(timestamp, action, params, result, reasoning)
                return result
            try:
                result = self._vision_handler.handle(action, params)
            except Exception as exc:
                result = {"success": False, "message": f"ACTION: {action} — VISION ERROR: {exc}"}
            self._log(timestamp, action, params, result, reasoning)
            return result

        # Route view/file-tree actions to ViewActionHandler
        if action in _VIEW_ACTIONS:
            if not self._view_handler:
                result = {"success": False, "message": f"ACTION: {action} — view_actions module not available (project_db required)"}
                self._log(timestamp, action, params, result, reasoning)
                return result
            try:
                result = self._view_handler.handle(action, params, session_id=self._session_id)
            except Exception as exc:
                result = {"success": False, "message": f"ACTION: {action} — VIEW ERROR: {exc}"}
            self._log(timestamp, action, params, result, reasoning)
            return result

        # Dispatch to handler
        handler = getattr(self, f"_do_{action}", None)
        if not handler:
            result = {"success": False, "message": f"ACTION: {action} — UNKNOWN ACTION"}
            self._log(timestamp, action, params, result, reasoning)
            return result

        try:
            result = handler(params)
        except Exception as exc:
            result = {"success": False, "message": f"ACTION: {action} — ERROR: {exc}"}

        # Log file write/edit actions to DB if handler is available
        if self._db_handler and result.get("success") and action in ("write_file", "edit_file"):
            try:
                from aixyad_db import log_action as _log_action
                rel_path = params.get("path", "")
                mod_action = "file_create" if action == "write_file" else "file_edit"
                _log_action(
                    self._db_handler.pdb, 'file', 0,
                    self._session_actor or 'agent', mod_action,
                    detail={'path': rel_path},
                    session_id=self._session_id,
                )
            except Exception:
                pass  # DB logging is best-effort

        self._log(timestamp, action, params, result, reasoning)
        return result

    # ── File Operations ─────────────────────────────────────────────────

    def _do_read_file(self, params: dict) -> dict:
        """Read a file and return its full content."""
        rel_path = params.get("path", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", 0)

        full_path = (self.project_dir / rel_path).resolve()

        if not full_path.is_file():
            return {"success": False, "message": f"ACTION: read_file {rel_path} — FILE NOT FOUND"}

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"success": False, "message": f"ACTION: read_file {rel_path} — ERROR: {exc}"}

        # Mark this path as inspected. Unblocks the "keep separate" opt-out
        # in write_file's basename-collision gate — agent can only assert
        # "these are genuinely different" AFTER they've read the canonical.
        try:
            self._inspected_paths.add(
                str(full_path.relative_to(self.project_dir)).replace("\\", "/").lower()
            )
        except Exception:
            pass

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        if offset or limit:
            end = (offset + limit) if limit else total_lines
            lines = lines[offset:end]
            content = "".join(lines)
            range_note = f" (lines {offset + 1}-{min(offset + len(lines), total_lines)} of {total_lines})"
        else:
            range_note = f" ({total_lines} lines)"

        # Record file read hash for freshness checking
        if self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb:
            try:
                from aixyad_db import record_file_read
                record_file_read(self._db_handler.pdb, rel_path,
                                  self._session_id, project_dir=self.project_dir)
            except Exception:
                pass

        return {
            "success": True,
            "message": (
                f"ACTION: read_file {rel_path} — SUCCESS{range_note}\n\n"
                f"--- FILE CONTENT ---\n{content}\n--- END FILE ---"
            ),
        }

    # Source-extension files belong under one of these directories.
    # Independent of whether the directory currently exists — the rule is
    # always-on, so the agent can't sneak paddle.py to root before src/
    # exists and then dodge the ban.
    SOURCE_DIRS = ("src", "lib", "include", "app", "core", "tests", "test",
                    "examples", "scripts")
    SOURCE_EXTS = (".py", ".pyi", ".cpp", ".hpp", ".h", ".cc", ".c",
                    ".rs", ".ts", ".tsx", ".js", ".jsx", ".go", ".java",
                    ".cs", ".kt")
    # Files with non-source extensions that ARE allowed at the project root.
    # Anything else at root must be a build/config/docs file, not source.
    ROOT_ALLOWED_BASENAMES = frozenset({
        "readme.md", "license", "license.md", "license.txt",
        "claude.md", "standards.md", "project_standards.md",
        "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
        "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
        ".gitignore", ".gitattributes", ".editorconfig", ".env.example",
        "build.bat", "build.sh", "makefile", "cmakelists.txt",
        "package.json", "package-lock.json", "tsconfig.json",
        "cargo.toml", "cargo.lock",
        "go.mod", "go.sum",
        "dockerfile", ".dockerignore",
        "agents.md",
    })
    # Positive allowlist for root-level subdirectories. Anything else at
    # depth-1 is banned — the model otherwise invents paddle/, paddle_ai/,
    # paddle_game/, pong_modules/, core_v2/ etc. The named-bans approach
    # only catches the pattern after we've seen it once.
    ALLOWED_ROOT_DIRS = frozenset({
        # Source / tests / library code
        "src", "tests", "test", "lib", "include", "app", "core",
        "scripts", "examples", "samples", "benchmarks", "internal",
        # Config / docs / data
        "config", "configs", "conf", "settings",
        "docs", "doc", "documentation",
        "assets", "data", "fixtures", "resources", "static", "templates",
        # Build / output (we don't write here but the agent may try)
        "build", "dist", "out", "target",
        # AIxYad-specific
        "_Discarded",
        # Common per-language directories
        "cmd", "pkg",                   # Go
        "bin",                          # binaries
    })

    def _layout_violation(self, rel_path: str) -> Optional[str]:
        """Return a human-readable rejection reason if rel_path violates
        the project layout rules, else None.

        Rule set is RULE-BASED, not state-based — i.e. the bans don't
        depend on whether src/ already exists. Otherwise the model can
        race the check by writing to root before src/ is created. Always-on:

          * Source files (.py / .cpp / .ts / etc.) at the project root
            (depth 1) — banned. Must live under src/, tests/, etc.
          * Project name as a subdirectory at root (Pong/main.py for the
            Pong project) — banned. Use src/.
          * Project-name + source extension (pong.py for Pong) — banned.
            Use src/main.py.
          * Banned sibling subfolders (paddle/, ai/, utils/, …) at root —
            banned. Use src/<name>/.
          * Non-source files at root must be in ROOT_ALLOWED_BASENAMES.
            Catches the agent dropping random .json / .yaml / .ini at
            root instead of in config/.
        """
        norm = rel_path.replace("\\", "/").lstrip("./")
        suffix = Path(norm).suffix.lower()
        parts = norm.split("/")
        proj_lower = self.project_dir.name.lower()

        # Project-name as filename at root: pong.py for Pong project.
        if len(parts) == 1 and Path(norm).stem.lower() == proj_lower:
            return (f"Layout rule: '{norm}' duplicates the project name as a "
                    f"root-level entry point. Use src/main.py instead.")

        # Source file at root (any source extension at depth 1) — banned.
        if len(parts) == 1 and suffix in self.SOURCE_EXTS:
            return (f"Layout rule: source files belong under src/, tests/, "
                    f"or another source directory — not at the project root. "
                    f"Move '{norm}' to src/{norm} (or src/<subdir>/{norm} or "
                    f"tests/{norm} if it's a test).")

        # Project-name as subdirectory: Pong/main.py for Pong project.
        if len(parts) >= 2 and parts[0].lower() == proj_lower:
            return (f"Layout rule: '{parts[0]}/' (a subfolder named after the "
                    f"project) is banned. Put '{Path(norm).name}' under "
                    f"src/ instead.")

        # Positive allowlist for root-level directories. Anything not in
        # ALLOWED_ROOT_DIRS at depth 1 is banned — closes the "agent invents
        # a new sibling folder name we hadn't blacklisted" loophole.
        if len(parts) >= 2 and parts[0].lower() not in self.ALLOWED_ROOT_DIRS:
            return (f"Layout rule: '{parts[0]}/' is not a recognised "
                    f"project-root directory. Allowed root dirs are: "
                    f"src/, tests/, lib/, app/, scripts/, examples/, "
                    f"config/, docs/, assets/, data/. "
                    f"Put '{Path(norm).name}' under src/{parts[0]}/{Path(norm).name} "
                    f"or merge the logic into an existing module under src/.")

        # Catch-all for non-source files at root that aren't allowed.
        # Lets src/, tests/, config/, docs/ subdirectories be anything,
        # but root must be a recognised build/config/docs file.
        if len(parts) == 1 and norm.lower() not in self.ROOT_ALLOWED_BASENAMES:
            # Allow common simple .md / .txt at root (auto-generated docs etc).
            if suffix in (".md", ".txt", ".rst"):
                return None
            return (f"Layout rule: '{norm}' isn't a recognised root-level "
                    f"file. Standard root-level files are README.md, "
                    f"STANDARDS.md, requirements.txt, pyproject.toml, "
                    f"build.bat, .gitignore, etc. Source goes under src/, "
                    f"config goes under config/, docs under docs/.")

        return None

    def _do_write_file(self, params: dict) -> dict:
        """Create a new file or overwrite an existing one.

        Auto-upgrades to overwrite=true when the file already exists, so agents
        don't loop retrying a denied write. Also injects a GUID comment header
        into .py files that don't already have one.
        """
        rel_path = params.get("path", "")
        content = params.get("content", "")

        # Layout enforcement — must come before the write. Edits to EXISTING
        # files are fine (handled in _do_edit_file); this only blocks NEW
        # files at banned paths so the model can't create paddle.py at root
        # or game/main.py when src/main.py is the canonical entry point.
        full_path_check = (self.project_dir / rel_path).resolve()
        if not full_path_check.exists():
            violation = self._layout_violation(rel_path)
            if violation:
                return {
                    "success": False,
                    "message": (
                        f"ACTION: write_file {rel_path} — DENIED ({violation}) "
                        f"Use edit_file on the existing canonical path, or "
                        f"write_file to the corrected path."
                    ),
                }

            # ── HARD: tests must NOT live inside src/ OR at root test/ ────
            # Caught the actual Pong-run sprawl: src/test/, src/tests/,
            # src/test_*.py, src/paddle_tests.py, AND test/ (singular) at
            # project root. Only tests/ (plural) at root is canonical.
            _rel_norm = rel_path.replace("\\", "/").lower()
            _basename_lc = full_path_check.name.lower()
            _looks_like_test = (
                _basename_lc.startswith("test_")
                or _basename_lc.endswith("_test.py")
                or _basename_lc.endswith("_tests.py")
            )
            if _rel_norm.startswith("src/") and (
                "/test/" in "/" + _rel_norm or "/tests/" in "/" + _rel_norm
                or _looks_like_test
            ):
                return {
                    "success": False,
                    "message": (
                        f"ACTION: write_file {rel_path} — DENIED (tests must "
                        f"live under tests/, not src/). Rewrite to "
                        f"tests/{full_path_check.name}."
                    ),
                }
            if _rel_norm.startswith("test/"):
                # Singular test/ at project root — wrong. Canonical is tests/.
                return {
                    "success": False,
                    "message": (
                        f"ACTION: write_file {rel_path} — DENIED (root-level "
                        f"test/ directory is non-canonical; use tests/ "
                        f"(plural)). Rewrite to "
                        f"tests/{rel_path[len('test/'):]}."
                    ),
                }

            # ── HARD: temp/scratch files banned ──────────────────────────
            # Caught the Pong sprawl: src/_temp/, src/temp_game.py,
            # src/_temp_game.py, src/temp.cpp, src/temp.txt. The agent has
            # take_note for scratchpad thinking; temp files leak into the
            # repo and never get cleaned up.
            _path_parts = [p.lower() for p in
                            full_path_check.relative_to(self.project_dir).parts]
            for _part in _path_parts:
                if _part in self.TEMP_DIR_NAMES:
                    return {
                        "success": False,
                        "message": (
                            f"ACTION: write_file {rel_path} — DENIED "
                            f"(directory {_part!r} is a temp/scratch path). "
                            f"Use take_note for scratchpad thinking, or "
                            f"write to the canonical location directly."
                        ),
                    }
            for _pat in self.TEMP_FILE_PATTERNS:
                if _pat.match(_basename_lc):
                    return {
                        "success": False,
                        "message": (
                            f"ACTION: write_file {rel_path} — DENIED "
                            f"(filename matches temp/scratch pattern). "
                            f"Use take_note for scratch thinking. If this "
                            f"is real code, rename to a meaningful "
                            f"name first."
                        ),
                    }

            # ── HARD: language allowlist ─────────────────────────────────
            # Caught the Pong sprawl: src/main.cpp, src/temp.cpp in a
            # Python project. If the project has a detected language,
            # writes outside its extension allowlist are rejected.
            if self._language and self._language in self.LANGUAGE_EXTENSIONS:
                _allowed_exts = self.LANGUAGE_EXTENSIONS[self._language]
                _suffix = full_path_check.suffix.lower()
                if _suffix and _suffix not in _allowed_exts:
                    return {
                        "success": False,
                        "message": (
                            f"ACTION: write_file {rel_path} — DENIED "
                            f"(file extension {_suffix!r} is outside the "
                            f"allowlist for language={self._language!r}). "
                            f"This is a {self._language} project; do not "
                            f"create files in other languages. Allowed "
                            f"extensions: "
                            f"{', '.join(sorted(_allowed_exts))}."
                        ),
                    }

            # ── Basename collision: hard-fail unless legitimate or ack'd ──
            # Caught the Pong sprawl: 3 ball.py, paddle code in 7 places.
            # Most collisions are mistakes (force edit_file on canonical).
            # Some are legitimate — auto-allow those. Otherwise the
            # rejection message includes a peek at the existing file plus
            # an explicit opt-out (params.acknowledge_duplicate=True +
            # params.reason="...") for the rare valid case.
            _basename = full_path_check.name
            _basename_lc = _basename.lower()

            # Auto-allowed legitimate collisions (don't even check):
            #   • __init__.py, conftest.py, _folder_summary.md — meant to repeat
            #   • test_<x>.py in tests/ when src/<x>.py exists (mirror pattern)
            _is_test_mirror = (
                _rel_norm.startswith("tests/")
                and (_basename_lc.startswith("test_") or _basename_lc.endswith("_test.py"))
            )
            _auto_allowed_basenames = {"__init__.py", "conftest.py",
                                       "_folder_summary.md", "readme.md"}
            _skip_basename_check = (
                _basename_lc in _auto_allowed_basenames or _is_test_mirror
            )

            # Special case: __init__.py is normally auto-allowed (legitimate
            # repetition), but NOT when a sibling <dir>.py already exists at
            # the parent — that's the file that *creates* a colliding
            # subpackage (src/paddle.py + src/paddle/__init__.py). Block.
            if (_basename_lc == "__init__.py"
                    and full_path_check.parent != self.project_dir.resolve()):
                _pkg_stem = full_path_check.parent.name
                _grandparent = full_path_check.parent.parent
                for _ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"):
                    _sib = _grandparent / f"{_pkg_stem}{_ext}"
                    if _sib.is_file():
                        _rel_sib = _sib.relative_to(self.project_dir)
                        return {
                            "success": False,
                            "message": (
                                f"ACTION: write_file {rel_path} — DENIED "
                                f"(creating __init__.py here would make "
                                f"{_pkg_stem}/ a subpackage that collides "
                                f"with existing sibling module "
                                f"{str(_rel_sib).replace(chr(92),'/')!s}). "
                                f"Either edit_file the existing module to "
                                f"add what you wanted, or discard the "
                                f"existing module first if the package "
                                f"shape is the right one."
                            ),
                        }

            if not _skip_basename_check:
                try:
                    _existing_dups: list[Path] = []
                    for _existing in self.project_dir.rglob(_basename):
                        if _existing.resolve() == full_path_check:
                            continue
                        _parts = _existing.relative_to(self.project_dir).parts
                        if any(p in self.HARD_FORBIDDEN_PATH_PARTS for p in _parts):
                            continue
                        if "_Discarded" in _parts:
                            continue
                        _existing_dups.append(_existing)
                        if len(_existing_dups) >= 3:
                            break

                    if _existing_dups:
                        # Opt-out: agent explicitly acknowledged this is
                        # legitimately separate AFTER actually inspecting the
                        # canonical (read_file or merge_files). Without that
                        # inspection check, small models would just always pass
                        # acknowledge_duplicate=True to bypass.
                        _first_rel_lc = str(
                            _existing_dups[0].relative_to(self.project_dir)
                        ).replace("\\", "/").lower()
                        _ack = bool(params.get("acknowledge_duplicate") and params.get("reason"))
                        _has_inspected = _first_rel_lc in self._inspected_paths
                        if _ack and _has_inspected:
                            pass  # allowed; fall through to write
                        elif _ack and not _has_inspected:
                            # Tried to skip the inspection. Reject with a
                            # specific instruction.
                            return {
                                "success": False,
                                "message": (
                                    f"ACTION: write_file {rel_path} — DENIED "
                                    f"(acknowledge_duplicate=true requires you "
                                    f"to first inspect the existing file at "
                                    f"{_first_rel_lc}. Call read_file or "
                                    f"merge_files on it, then re-attempt "
                                    f"write_file with acknowledge_duplicate "
                                    f"and reason.)"
                                ),
                            }
                        else:
                            # Surface a peek of the FIRST existing file +
                            # echo the agent's submitted content back so the
                            # agent can decide in ONE turn without losing work.
                            _first = _existing_dups[0]
                            try:
                                _peek_lines = _first.read_text(
                                    encoding="utf-8", errors="replace"
                                ).splitlines()
                                _existing_total = len(_peek_lines)
                                _peek_text = "\n  ".join(_peek_lines[:30])
                                if _existing_total > 30:
                                    _peek_text += f"\n  ...({_existing_total - 30} more lines)"
                            except Exception:
                                _peek_text = "(could not read)"
                                _existing_total = 0
                            _locs = ", ".join(
                                str(p.relative_to(self.project_dir)).replace("\\", "/")
                                for p in _existing_dups
                            )
                            _first_rel = str(
                                _first.relative_to(self.project_dir)
                            ).replace("\\", "/")

                            # Echo the submitted content back so agent doesn't
                            # have to regenerate. Cap at 80 lines so token
                            # budget stays sane on big writes.
                            _submitted_lines = (content or "").splitlines()
                            _submitted_total = len(_submitted_lines)
                            _submitted_preview = "\n  ".join(_submitted_lines[:80])
                            if _submitted_total > 80:
                                _submitted_preview += f"\n  ...({_submitted_total - 80} more lines truncated)"

                            return {
                                "success": False,
                                "message": (
                                    f"ACTION: write_file {rel_path} — DENIED "
                                    f"(a file named {_basename!r} already exists "
                                    f"at: {_locs})\n\n"
                                    f"=== EXISTING file: {_first_rel} "
                                    f"({_existing_total} lines) ===\n"
                                    f"  {_peek_text}\n\n"
                                    f"=== YOUR submitted content for {rel_path} "
                                    f"({_submitted_total} lines, preserved below) ===\n"
                                    f"  {_submitted_preview}\n\n"
                                    f"Recommended next action — pick one:\n"
                                    f"  (1) MERGE: call edit_file on "
                                    f"{_first_rel} with old_text=<a snippet "
                                    f"from the existing file above> and "
                                    f"new_text=<that snippet + the relevant "
                                    f"part of your submitted content>. This "
                                    f"is the right call for refactor/extract/"
                                    f"rename/polish tasks.\n"
                                    f"  (2) REPLACE the existing file: call "
                                    f"write_file on {_first_rel} (the "
                                    f"existing path) with your submitted "
                                    f"content — overwrite is auto-enabled.\n"
                                    f"  (3) KEEP SEPARATE: re-call write_file "
                                    f"on {rel_path} with "
                                    f"params.acknowledge_duplicate=true and "
                                    f"params.reason=\"<why these are "
                                    f"genuinely different concerns>\"."
                                ),
                            }
                except Exception:
                    pass  # best-effort

            # ── HARD: ANY ancestor dir collides with sibling module ──────
            # Walks every directory between project root and the file's
            # parent. For each ancestor dir <ancestor_name>, checks if
            # <grandparent>/<ancestor_name>.<ext> exists. Catches both:
            #   • write src/paddle/x.py while src/paddle.py exists
            #   • write src/game/components/enemy.py while src/game.py exists
            # The previous version only checked the immediate parent and
            # missed nested writes once a __init__.py created the subdir.
            try:
                _project_root = self.project_dir.resolve()
                _ancestor = full_path_check.parent
                while _ancestor != _project_root and _ancestor.parent != _ancestor:
                    _stem = _ancestor.name
                    _grandparent = _ancestor.parent
                    for _ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"):
                        _sibling = _grandparent / f"{_stem}{_ext}"
                        if _sibling.is_file():
                            _rel_sib = str(
                                _sibling.relative_to(self.project_dir)
                            ).replace("\\", "/")
                            return {
                                "success": False,
                                "message": (
                                    f"ACTION: write_file {rel_path} — DENIED "
                                    f"(directory {_stem}/ in this path "
                                    f"collides with existing sibling module "
                                    f"{_rel_sib}). The whole subtree under "
                                    f"{_stem}/ is invalid while {_rel_sib} "
                                    f"exists. Either edit_file {_rel_sib} to "
                                    f"add what you need, or discard "
                                    f"{_rel_sib} first if the package shape "
                                    f"is correct."
                                ),
                            }
                    _ancestor = _ancestor.parent
                    # Don't escape the project root
                    if _project_root not in _ancestor.parents and _ancestor != _project_root:
                        break
            except Exception:
                pass

        full_path = (self.project_dir / rel_path).resolve()

        # Auto-upgrade: if file exists and overwrite not explicitly set, allow it
        # (prevents infinite retry loops when agents forget overwrite=true)
        if full_path.exists() and not params.get("overwrite", False):
            params = {**params, "overwrite": True}

        # Inject GUID header into new source files that don't already have one
        _ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
        _already_has_guid = "# GUID:" in content or "// GUID:" in content
        if content and not _already_has_guid:
            _guid = str(uuid.uuid4())
            if _ext == "py":
                content = f"# GUID: {_guid}\n{content}"
            elif _ext in ("js", "ts", "tsx", "jsx", "cpp", "hpp", "h", "c", "go", "rs", "java"):
                content = f"// GUID: {_guid}\n{content}"

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with _os_file_lock(full_path):
                full_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"success": False, "message": f"ACTION: write_file {rel_path} — ERROR: {exc}"}

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        # Enqueue for change-driven summary refresh — drained by SprintRunner
        # at sprint end. No-op if no indexer is attached.
        if self._summary_indexer is not None:
            try:
                self._summary_indexer.enqueue(full_path)
            except Exception:
                pass

        return {
            "success": True,
            "message": f"ACTION: write_file {rel_path} — SUCCESS ({line_count} lines written)",
        }

    def _do_edit_file(self, params: dict) -> dict:
        """Replace text in an existing file. Checks file lock and freshness first."""
        rel_path = params.get("path", "")
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")

        if not old_text:
            return {"success": False, "message": f"ACTION: edit_file {rel_path} — ERROR: old_text is empty"}

        full_path = (self.project_dir / rel_path).resolve()

        if not full_path.is_file():
            return {"success": False, "message": f"ACTION: edit_file {rel_path} — FILE NOT FOUND"}

        # Check file lock — if locked by another agent, deny edit
        if self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb:
            try:
                from aixyad_db import check_file_lock, check_file_freshness
                lock = check_file_lock(self._db_handler.pdb, rel_path)
                if lock and lock['locked_by'] != self._session_actor:
                    return {"success": False,
                            "message": f"ACTION: edit_file {rel_path} — LOCKED by {lock['locked_by']} "
                                       f"(since {lock['locked_at']}). Wait or use a different file."}

                # Check freshness — if file changed since last read, force re-read
                freshness = check_file_freshness(self._db_handler.pdb, rel_path,
                                                   self._session_id, project_dir=self.project_dir)
                if not freshness.get('fresh', True) and freshness.get('old_hash'):
                    return {"success": False,
                            "message": f"ACTION: edit_file {rel_path} — FILE CHANGED since you last read it. "
                                       f"Use read_file to get the current content before editing."}
            except ImportError:
                pass
            except Exception:
                pass  # Best-effort checks

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"success": False, "message": f"ACTION: edit_file {rel_path} — ERROR: {exc}"}

        occurrences = content.count(old_text)
        if occurrences == 0:
            # Try to diagnose WHY the old_text wasn't found
            hint = self._diagnose_edit_mismatch(content, old_text, rel_path)
            return {
                "success": False,
                "message": (
                    f"ACTION: edit_file {rel_path} — old_text NOT FOUND in file.\n"
                    f"{hint}\n"
                    f"HINT: Use read_file first to see the exact file contents, "
                    f"then copy the exact text (including whitespace, quotes, newlines) into old_text."
                ),
            }

        new_content = content.replace(old_text, new_text)

        try:
            with _os_file_lock(full_path):
                # Re-read inside the lock window to defend against any concurrent
                # write that landed between our earlier read and this edit.
                fresh_content = full_path.read_text(encoding="utf-8", errors="replace")
                if fresh_content != content:
                    return {
                        "success": False,
                        "message": (
                            f"ACTION: edit_file {rel_path} — RACE: file changed "
                            f"under us between read and write. Re-read and retry."
                        ),
                    }
                full_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return {"success": False, "message": f"ACTION: edit_file {rel_path} — WRITE ERROR: {exc}"}

        old_lines = content.count("\n")
        new_lines = new_content.count("\n")
        delta = new_lines - old_lines

        # Record as a "read" so freshness checks pass on subsequent edits in this session
        if self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb:
            try:
                from aixyad_db import record_file_read
                record_file_read(self._db_handler.pdb, rel_path,
                                 self._session_id, project_dir=self.project_dir)
            except Exception:
                pass

        # Enqueue for change-driven summary refresh.
        if self._summary_indexer is not None:
            try:
                self._summary_indexer.enqueue(full_path)
            except Exception:
                pass

        return {
            "success": True,
            "message": (
                f"ACTION: edit_file {rel_path} — SUCCESS "
                f"(replaced {occurrences} occurrence{'s' if occurrences != 1 else ''}, "
                f"{abs(delta)} lines {'added' if delta >= 0 else 'removed'})"
            ),
        }

    def _do_copy_file(self, params: dict) -> dict:
        """Copy a file inside the project. Both src and dst are validated by
        _check_path so neither can escape project_dir or land inside a
        forbidden system directory (.git, .aixyad, venv, etc.)."""
        src = params.get("src", "") or params.get("source", "")
        dst = params.get("dst", "") or params.get("dest", "") or params.get("destination", "")
        if not src or not dst:
            return {"success": False,
                    "message": "ACTION: copy_file — ERROR: src and dst are both required"}
        for label, p in (("src", src), ("dst", dst)):
            check = self._check_path(p)
            if not check["safe"]:
                return {"success": False,
                        "message": f"ACTION: copy_file {label}={p} — DENIED ({check['reason']})"}
        src_full = (self.project_dir / src).resolve()
        dst_full = (self.project_dir / dst).resolve()
        if not src_full.is_file():
            return {"success": False,
                    "message": f"ACTION: copy_file {src} — SOURCE NOT FOUND"}
        if dst_full.exists() and not params.get("overwrite", False):
            return {"success": False,
                    "message": (f"ACTION: copy_file {src} -> {dst} — DEST EXISTS "
                                f"(pass overwrite=true to replace)")}
        try:
            dst_full.parent.mkdir(parents=True, exist_ok=True)
            with _os_file_lock(dst_full):
                import shutil as _shutil
                _shutil.copy2(src_full, dst_full)
        except OSError as exc:
            return {"success": False,
                    "message": f"ACTION: copy_file {src} -> {dst} — ERROR: {exc}"}
        # Enqueue for change-driven summary refresh
        if self._summary_indexer is not None:
            try:
                self._summary_indexer.enqueue(dst_full)
            except Exception:
                pass
        return {"success": True,
                "message": f"ACTION: copy_file {src} -> {dst} — SUCCESS"}

    def _do_merge_files(self, params: dict) -> dict:
        """Consolidate a duplicate file into a canonical one.

        Three input shapes:
          (a) target + source: read both, merge source's non-duplicate
              top-level imports/defs/classes into target, discard source.
          (b) target + new_content: same merge logic but using a literal
              content string (e.g. content the agent had drafted but
              write_file rejected for collision).
          (c) target only: just inspect target — useful as the "step that
              proves I looked" before declaring acknowledge_duplicate=true.

        Always marks target (and source if provided) as inspected so a
        subsequent write_file with acknowledge_duplicate is allowed.
        """
        target = params.get("target", "") or params.get("dest", "") or params.get("dst", "")
        source = params.get("source", "") or params.get("src", "")
        new_content = params.get("new_content", "")

        if not target:
            return {"success": False,
                    "message": "ACTION: merge_files — ERROR: target is required"}

        for label, p in (("target", target), ("source", source)):
            if not p:
                continue
            check = self._check_path(p)
            if not check["safe"]:
                return {"success": False,
                        "message": f"ACTION: merge_files {label}={p} — DENIED ({check['reason']})"}

        target_full = (self.project_dir / target).resolve()
        if not target_full.is_file():
            return {"success": False,
                    "message": f"ACTION: merge_files target={target} — TARGET NOT FOUND"}

        try:
            target_text = target_full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"success": False,
                    "message": f"ACTION: merge_files {target} — ERROR reading target: {exc}"}

        # Mark target inspected immediately — even pure-inspect calls count.
        try:
            self._inspected_paths.add(
                str(target_full.relative_to(self.project_dir)).replace("\\", "/").lower()
            )
        except Exception:
            pass

        # Determine the source content
        source_text: str = ""
        source_full = None
        if source:
            source_full = (self.project_dir / source).resolve()
            if not source_full.is_file():
                return {"success": False,
                        "message": f"ACTION: merge_files source={source} — SOURCE NOT FOUND"}
            try:
                source_text = source_full.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                return {"success": False,
                        "message": f"ACTION: merge_files {source} — ERROR reading source: {exc}"}
            try:
                self._inspected_paths.add(
                    str(source_full.relative_to(self.project_dir)).replace("\\", "/").lower()
                )
            except Exception:
                pass
        elif new_content:
            source_text = new_content
        else:
            # Inspect-only mode. Return target structure so the agent can
            # decide what to do next.
            target_lines = target_text.splitlines()
            preview = "\n  ".join(target_lines[:60])
            tail = ""
            if len(target_lines) > 60:
                tail = f"\n  ...({len(target_lines) - 60} more lines)"
            return {"success": True,
                    "message": (
                        f"ACTION: merge_files {target} — INSPECTED (no source given). "
                        f"target has {len(target_lines)} lines. First 60:\n  "
                        f"{preview}{tail}\n\n"
                        f"Now you can: (a) edit_file {target} to add new logic, "
                        f"(b) call merge_files again with source/new_content to "
                        f"merge automatically, or (c) write_file at a different "
                        f"path with acknowledge_duplicate=true if these are "
                        f"genuinely separate concerns."
                    )}

        # ── Smart merge (Python-only AST diff; falls back to safe append) ─
        merged_text, merge_summary = self._smart_merge_python(
            target_text, source_text, target_path=target, source_path=source or "<inline>"
        )

        # Write the merged content back to target
        try:
            with _os_file_lock(target_full):
                target_full.write_text(merged_text, encoding="utf-8")
        except OSError as exc:
            return {"success": False,
                    "message": f"ACTION: merge_files {target} — ERROR writing: {exc}"}

        # Discard the source file (soft-delete) if it was a real path
        discarded_msg = ""
        if source_full is not None:
            try:
                from datetime import datetime, timezone
                # Move to _Discarded/ with timestamp + original-path suffix
                disc_root = self.project_dir / "_Discarded"
                disc_root.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                rel_src_str = str(source_full.relative_to(self.project_dir)).replace("/", "_").replace("\\", "_")
                disc_path = disc_root / f"{ts}__{rel_src_str}"
                source_full.rename(disc_path)
                discarded_msg = f" Discarded source -> _Discarded/{disc_path.name}"
            except Exception as exc:
                discarded_msg = f" (NOTE: source still on disk; could not discard: {exc})"

        # Enqueue summary refresh
        if self._summary_indexer is not None:
            try:
                self._summary_indexer.enqueue(target_full)
            except Exception:
                pass

        return {"success": True,
                "message": (
                    f"ACTION: merge_files {source or '<inline>'} -> {target} — "
                    f"SUCCESS. {merge_summary}.{discarded_msg}"
                )}

    def _smart_merge_python(self, target_text: str, source_text: str,
                              target_path: str = "", source_path: str = "") -> tuple:
        """Merge source Python content into target. Returns (merged_text, summary).

        Strategy:
          1. Parse both with ast. If parsing fails for either, fall back to
             safe append with a separator comment.
          2. Index target's top-level names (functions, classes, imports).
          3. For each top-level node in source:
             - skip if its name is already in target (avoid duplicate defs)
             - keep imports that are already present (deduped)
             - append unique top-level statements to the end of target
        """
        import ast as _ast

        # Try Python AST merge first
        try:
            target_tree = _ast.parse(target_text)
            source_tree = _ast.parse(source_text)
        except Exception:
            # Non-Python or parse failure: safe append
            sep = (f"\n\n# ── merged from {source_path} "
                   f"({len(source_text.splitlines())} lines) ──\n")
            merged = target_text.rstrip() + sep + source_text.lstrip()
            return merged, "merged via append (one or both files not parseable Python)"

        # Index target's top-level names + imports
        target_names: set = set()
        target_imports: set = set()
        for node in target_tree.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                target_names.add(node.name)
            elif isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        target_names.add(t.id)
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    target_imports.add(alias.name)
            elif isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    target_imports.add(f"{node.module}.{alias.name}" if node.module else alias.name)

        # Walk source's top-level nodes, keep only unique ones
        new_imports_src: list = []
        new_defs_src: list = []
        skipped: list = []
        source_lines = source_text.splitlines()

        def _node_src(n) -> str:
            try:
                return _ast.get_source_segment(source_text, n) or ""
            except Exception:
                return ""

        for node in source_tree.body:
            seg = _node_src(node)
            if not seg.strip():
                continue
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name not in target_imports:
                        new_imports_src.append(seg)
                        target_imports.add(alias.name)
                        break
            elif isinstance(node, _ast.ImportFrom):
                key = f"{node.module}.{node.names[0].name}" if node.module else node.names[0].name
                if key not in target_imports:
                    new_imports_src.append(seg)
                    target_imports.add(key)
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                if node.name in target_names:
                    skipped.append(node.name)
                else:
                    new_defs_src.append(seg)
                    target_names.add(node.name)
            elif isinstance(node, _ast.Assign):
                # Append assignments unconditionally (they might be config the
                # agent wants to add). But if the LHS name already exists, skip.
                tgt_names = [t.id for t in node.targets if isinstance(t, _ast.Name)]
                if any(n in target_names for n in tgt_names):
                    skipped.extend(tgt_names)
                else:
                    new_defs_src.append(seg)
                    target_names.update(tgt_names)
            else:
                # Other top-level statements (e.g. expressions, if-blocks):
                # append them so the agent's intent isn't lost.
                new_defs_src.append(seg)

        # Build the merged file: existing target + new imports (after existing
        # imports if possible — we just append at end for safety) + new defs.
        merged_parts = [target_text.rstrip()]
        if new_imports_src:
            merged_parts.append("\n# ── merged imports ──\n" + "\n".join(new_imports_src))
        if new_defs_src:
            merged_parts.append("\n\n# ── merged definitions ──\n\n"
                                + "\n\n".join(new_defs_src))
        merged_text = "\n".join(merged_parts) + "\n"

        summary = (
            f"added {len(new_imports_src)} import(s) and "
            f"{len(new_defs_src)} top-level definition(s); "
            f"skipped {len(skipped)} duplicate name(s)"
        )
        if skipped:
            summary += f" ({', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''})"
        return merged_text, summary

    def _do_take_note(self, params: dict) -> dict:
        """Append a note to the agent's persistent task scratchpad.

        Notes are stored at ``.aixyad/notes/<task_id>.md`` (private,
        gitignored, never executed) and auto-injected into the prompt for
        every iteration of the same task. Use this when:
          * you've made a non-obvious decision you'll forget
          * you've mapped out remaining work and don't want to re-derive it
          * you noticed something about the codebase worth remembering for
            the next iteration on this same task

        Params:
          text   — the note body.
          mode   — "append" (default) or "replace".
        """
        text = (params.get("text") or "").strip()
        if not text:
            return {"success": False,
                    "message": "ACTION: take_note — ERROR: text is required"}
        mode = (params.get("mode") or "append").lower()
        if mode not in ("append", "replace"):
            return {"success": False,
                    "message": f"ACTION: take_note — ERROR: mode must be 'append' or 'replace' (got {mode!r})"}

        # Identify the current task. The agent can override via the task_id
        # param; otherwise we use the executor's tracked current task. Falls
        # back to "shared" so notes never get silently lost.
        task_id = (params.get("task_id") or
                   self._current_task_id or
                   "shared")
        # Strip path-unsafe chars from task_id
        import re as _re
        safe_id = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))[:80] or "shared"

        notes_dir = aixyad_paths.project_state(self.project_dir) / "notes"
        try:
            notes_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"success": False,
                    "message": f"ACTION: take_note — ERROR: cannot create notes dir: {exc}"}
        notes_path = notes_dir / f"{safe_id}.md"

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n## {stamp}\n{text}\n"
        try:
            if mode == "replace":
                notes_path.write_text(f"# Notes for {safe_id}\n{entry}",
                                       encoding="utf-8")
            else:
                if notes_path.is_file():
                    with notes_path.open("a", encoding="utf-8") as f:
                        f.write(entry)
                else:
                    notes_path.write_text(f"# Notes for {safe_id}\n{entry}",
                                           encoding="utf-8")
        except OSError as exc:
            return {"success": False,
                    "message": f"ACTION: take_note — ERROR: write failed: {exc}"}
        size = notes_path.stat().st_size
        return {"success": True,
                "message": (f"ACTION: take_note ({mode}, task={safe_id}) — SUCCESS "
                            f"(notepad now {size}B). The next iteration's "
                            f"prompt will include all notes for this task.")}

    # ── Live Scanner Actions ──────────────────────────────────────────────

    def _do_query_file_structure(self, params: dict) -> dict:
        """Get file's functions/classes/sections with line numbers (live from disk)."""
        from project_scanner import get_file_structure
        file_path = params.get("file_path", "")
        if not file_path:
            return {"success": False, "message": "ACTION: query_file_structure — ERROR: file_path is required"}

        result = get_file_structure(self.project_dir, file_path)
        if result.get("error"):
            return {"success": False, "message": f"ACTION: query_file_structure {file_path} — {result['error']}"}

        # Format the structure into readable text
        lines = [
            f"ACTION: query_file_structure {file_path} — SUCCESS",
            f"Language: {result.get('language', '?')} | Lines: {result.get('line_count', 0)} | "
            f"Size: {result.get('size_bytes', 0)} bytes",
            "",
            "--- STRUCTURE ---",
        ]

        for label, key in [("Classes", "classes"), ("Functions", "functions"),
                           ("Enums", "enums"), ("Namespaces", "namespaces"),
                           ("Sections", "sections"), ("TODOs", "todos")]:
            items = result.get(key, [])
            if items:
                lines.append(f"\n{label} ({len(items)}):")
                for item in items:
                    name = item.get("name", item.get("text", "?"))
                    line_num = item.get("line", "?")
                    sig = item.get("signature", "")
                    extra = f"  — {sig}" if sig else ""
                    lines.append(f"  L{line_num}: {name}{extra}")

        lines.append("--- END STRUCTURE ---")
        return {"success": True, "message": "\n".join(lines)}

    def _do_query_symbols(self, params: dict) -> dict:
        """Search for functions/classes/keys across the project (live scan)."""
        from project_scanner import search_symbols
        name = params.get("name", "")
        symbol_type = params.get("symbol_type", None)
        file_glob = params.get("file_glob", None)

        results = search_symbols(
            self.project_dir,
            name=name or None,
            symbol_type=symbol_type or None,
            file_glob=file_glob or None,
        )

        lines = [f"ACTION: query_symbols — {len(results)} MATCHES", "", "--- SYMBOLS ---"]
        for r in results:
            sig = r.get("signature", "")
            extra = f"  — {sig}" if sig else ""
            lines.append(f"  {r.get('file', '?')}:L{r.get('line', '?')} [{r.get('type', '?')}] {r.get('name', '?')}{extra}")
        lines.append("--- END SYMBOLS ---")
        return {"success": True, "message": "\n".join(lines)}

    def _do_query_files(self, params: dict) -> dict:
        """List project files with filters (live from disk)."""
        from project_scanner import list_project_files
        extension = params.get("extension", None)
        path_contains = params.get("path_contains", None)

        results = list_project_files(
            self.project_dir,
            extension=extension or None,
            path_contains=path_contains or None,
        )

        lines = [f"ACTION: query_files — {len(results)} FILES", "", "--- FILES ---"]
        for f in results:
            lines.append(
                f"  {f.get('path', '?')} ({f.get('language', '?')}, "
                f"{f.get('line_count', 0)} lines, {f.get('size_bytes', 0)} bytes)"
            )
        lines.append("--- END FILES ---")
        return {"success": True, "message": "\n".join(lines)}

    def _do_search_by_guid(self, params: dict) -> dict:
        """Find all occurrences of a GUID tag across project files."""
        guid = params.get("guid", "").strip()
        if not guid:
            return {"success": False, "message": "ACTION: search_by_guid — ERROR: guid param required"}
        import re as _re
        pattern = _re.compile(
            r'(?:#|//)\s*(?:GUID|guid)[\s:]+' + _re.escape(guid), _re.IGNORECASE
        )
        results: list[str] = []
        for fp in self.project_dir.rglob("*"):
            if not fp.is_file():
                continue
            if any(p in fp.parts for p in ("__pycache__", ".aixyad", ".automatica", ".git", "node_modules")):
                continue
            if fp.suffix.lower() not in ('.py', '.js', '.ts', '.tsx', '.cpp', '.hpp',
                                          '.h', '.c', '.go', '.rs', '.java'):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = str(fp.relative_to(self.project_dir)).replace("\\", "/")
            for i, line in enumerate(lines, 1):
                if pattern.search(line):
                    ctx_start = max(0, i - 6)
                    ctx_end   = min(len(lines), i + 5)
                    ctx = "\n".join(f"  {ctx_start+j+1:4}: {lines[ctx_start+j]}"
                                    for j in range(ctx_end - ctx_start))
                    results.append(f"--- {rel}:{i} ---\n{ctx}")
        if not results:
            return {"success": True, "message": f"ACTION: search_by_guid — GUID '{guid}' not found"}
        header = f"ACTION: search_by_guid '{guid}' — {len(results)} match(es)"
        return {"success": True, "message": header + "\n\n" + "\n\n".join(results[:20])}

    def _do_query_wiki(self, params: dict) -> dict:
        """Return a compact file index sorted by recency."""
        from project_scanner import list_project_files
        limit = min(int(params.get("limit", 25)), 100)
        files = list_project_files(self.project_dir, limit=limit)
        files.sort(key=lambda f: f.get("mtime_epoch", 0.0), reverse=True)
        lines = [f"ACTION: query_wiki — {len(files)} files (most recent first)", "", "--- WIKI ---"]
        for f in files[:limit]:
            lines.append(
                f"  {f['path']}  [{f.get('language', '?')}]  "
                f"{f.get('line_count', 0)}L  {f.get('last_modified', '')}"
            )
        lines.append("--- END WIKI ---")
        return {"success": True, "message": "\n".join(lines)}

    def _do_get_summary(self, params: dict) -> dict:
        """Cheap fetch of a path's existing in-source summary.

        For .py / .pyi files: returns the module docstring (text only, no
        triple-quotes). For folders: returns the body of
        ``<folder>/_folder_summary.md``. For markdown files: returns the
        first paragraph. Use this BEFORE read_file when you only need
        orientation — avoids pulling whole files into context."""
        target = params.get("path", "").strip()
        if not target:
            return {"success": False,
                    "message": "ACTION: get_summary — ERROR: path is required"}
        full = (self.project_dir / target).resolve()
        if not full.exists():
            return {"success": False,
                    "message": f"ACTION: get_summary {target} — NOT FOUND"}

        if full.is_dir():
            summary_file = full / "_folder_summary.md"
            if summary_file.is_file():
                try:
                    body = summary_file.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    return {"success": False,
                            "message": f"ACTION: get_summary {target} — READ ERROR: {exc}"}
                return {"success": True,
                        "message": (f"ACTION: get_summary {target} (folder)\n\n"
                                    + body[:2000])}
            return {"success": False,
                    "message": (f"ACTION: get_summary {target} — no "
                                f"_folder_summary.md yet (will be auto-"
                                f"generated after a write/edit in this folder)")}

        suffix = full.suffix.lower()
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"success": False,
                    "message": f"ACTION: get_summary {target} — READ ERROR: {exc}"}

        if suffix in (".py", ".pyi"):
            try:
                from summary_indexer import _python_module_docstring
                import ast as _ast
                tree = _ast.parse(text)
                doc = _python_module_docstring(tree)
                if doc and doc.value.value:
                    return {"success": True,
                            "message": (f"ACTION: get_summary {target}\n\n"
                                        + doc.value.value[:2000])}
            except Exception:
                pass
            return {"success": False,
                    "message": (f"ACTION: get_summary {target} — no module "
                                f"docstring (will be auto-generated after a "
                                f"write/edit)")}

        if suffix in (".md", ".rst", ".txt"):
            # First paragraph (until blank line) — usually the orientation.
            chunks = re.split(r"\n\s*\n", text.strip(), maxsplit=1)
            return {"success": True,
                    "message": f"ACTION: get_summary {target}\n\n{chunks[0][:2000]}"}

        return {"success": False,
                "message": (f"ACTION: get_summary {target} — unsupported "
                            f"extension {suffix!r}; use read_file instead")}

    def _do_find_relevant_files(self, params: dict) -> dict:
        """Rank project files by relevance to a free-text query.

        Scoring is keyword-based — the query is tokenised, each token is
        matched against (a) the file path, (b) the module docstring if any,
        (c) any ``_folder_summary.md`` in the file's chain of parent folders.
        Cheap, deterministic, no embeddings. Returns the top-K ranked paths
        with a one-line snippet from each summary.
        """
        query = params.get("query", "").strip().lower()
        top_k = min(int(params.get("top_k", 10)), 30)
        if not query:
            return {"success": False,
                    "message": "ACTION: find_relevant_files — ERROR: query required"}

        from project_scanner import list_project_files
        try:
            from summary_indexer import (
                _python_module_docstring, SKIP_PATTERNS as _SKIP,
            )
        except Exception:
            _python_module_docstring = None  # type: ignore[assignment]
            _SKIP = ()

        terms = [t for t in re.split(r"\W+", query) if len(t) >= 3]
        if not terms:
            return {"success": False,
                    "message": "ACTION: find_relevant_files — ERROR: query too short"}

        files = list_project_files(self.project_dir) or []
        scored: list[tuple[float, str, str]] = []
        for f in files:
            rel = f.get("path", "")
            if not rel or rel.endswith(".lock"):
                continue
            if any(s in rel.replace("\\", "/") for s in _SKIP):
                continue
            full = self.project_dir / rel
            if not full.is_file():
                continue
            haystack = rel.lower()
            snippet = ""
            if _python_module_docstring and full.suffix.lower() in (".py", ".pyi"):
                try:
                    import ast as _ast
                    tree = _ast.parse(full.read_text(encoding="utf-8",
                                                     errors="replace"))
                    doc = _python_module_docstring(tree)
                    if doc and doc.value.value:
                        haystack += "\n" + doc.value.value.lower()
                        snippet = doc.value.value.split("\n")[0][:120]
                except Exception:
                    pass
            # Folder summary up the chain (one level).
            try:
                folder_summary = full.parent / "_folder_summary.md"
                if folder_summary.is_file():
                    haystack += "\n" + folder_summary.read_text(
                        encoding="utf-8", errors="replace").lower()
            except Exception:
                pass

            score = sum(haystack.count(t) for t in terms)
            # Bonus: query terms appearing in the path itself rank higher.
            score += sum(2 for t in terms if t in rel.lower())
            if score > 0:
                scored.append((float(score), rel, snippet))

        scored.sort(key=lambda r: r[0], reverse=True)
        if not scored:
            return {"success": True,
                    "message": (f"ACTION: find_relevant_files '{query}' — "
                                f"0 matches across {len(files)} files")}

        out = [f"ACTION: find_relevant_files '{query}' — top {min(top_k, len(scored))} of {len(scored)} matches"]
        for score, rel, snippet in scored[:top_k]:
            line = f"  [score={score:>4.1f}] {rel}"
            if snippet:
                line += f"\n    {snippet}"
            out.append(line)
        return {"success": True, "message": "\n".join(out)}

    def _do_query_file_section(self, params: dict) -> dict:
        """Read specific lines or GUID-tagged section from a file."""
        file_path = params.get("file_path", "")
        guid = params.get("guid", "")

        if not file_path:
            return {"success": False, "message": "ACTION: query_file_section — ERROR: file_path is required"}

        # GUID-based lookup: find lines between GUID marker and next GUID marker
        if guid:
            full_path = (self.project_dir / file_path).resolve()
            if not full_path.is_file():
                return {"success": False, "message": f"ACTION: query_file_section {file_path} — FILE NOT FOUND"}
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return {"success": False, "message": f"ACTION: query_file_section {file_path} — ERROR: {exc}"}

            lines = content.splitlines(keepends=True)
            guid_pattern = re.compile(r'(?:#|//)\s*GUID:\s*' + re.escape(guid), re.IGNORECASE)
            next_guid_pattern = re.compile(r'(?:#|//)\s*GUID:\s*[\w-]+', re.IGNORECASE)

            start_idx = None
            for i, line in enumerate(lines):
                if guid_pattern.search(line):
                    start_idx = i
                    break

            if start_idx is None:
                return {
                    "success": False,
                    "message": f"ACTION: query_file_section {file_path} — GUID '{guid}' NOT FOUND",
                }

            end_idx = len(lines)
            for i in range(start_idx + 1, len(lines)):
                if next_guid_pattern.search(lines[i]):
                    end_idx = i
                    break

            section_content = "".join(lines[start_idx:end_idx])
            range_info = f"lines {start_idx + 1}-{end_idx} of {len(lines)}"
            return {
                "success": True,
                "message": (
                    f"ACTION: query_file_section {file_path} guid={guid} — SUCCESS ({range_info})\n\n"
                    f"--- CONTENT ---\n{section_content}\n--- END CONTENT ---"
                ),
            }

        # Fall back to line-number / section-name based lookup
        from project_scanner import get_file_section
        line_start = params.get("line_start")
        line_end = params.get("line_end")
        section_name = params.get("section_name")

        # Convert string params to int if needed
        if isinstance(line_start, str) and line_start.isdigit():
            line_start = int(line_start)
        if isinstance(line_end, str) and line_end.isdigit():
            line_end = int(line_end)

        result = get_file_section(
            self.project_dir, file_path,
            line_start=line_start, line_end=line_end,
            section_name=section_name,
        )

        if result.get("error"):
            return {"success": False, "message": f"ACTION: query_file_section {file_path} — {result['error']}"}

        range_info = f"lines {result.get('line_start', '?')}-{result.get('line_end', '?')} of {result.get('total_lines', '?')}"
        return {
            "success": True,
            "message": (
                f"ACTION: query_file_section {file_path} — SUCCESS ({range_info})\n\n"
                f"--- CONTENT ---\n{result.get('content', '')}\n--- END CONTENT ---"
            ),
        }

    def _do_get_json_keys(self, params: dict) -> dict:
        """S3-W02: List top-level keys of a JSON config file with types and previews."""
        file_path = params.get("file_path", "")
        if not file_path:
            return {"success": False, "message": "ACTION: get_json_keys — ERROR: file_path is required"}
        full_path = (self.project_dir / file_path).resolve()
        if not full_path.is_file():
            return {"success": False, "message": f"ACTION: get_json_keys {file_path} — FILE NOT FOUND"}
        try:
            import json as _json
            data = _json.loads(full_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return {"success": False, "message": f"ACTION: get_json_keys {file_path} — PARSE ERROR: {exc}"}
        if not isinstance(data, dict):
            return {"success": False,
                    "message": f"ACTION: get_json_keys {file_path} — NOT A JSON OBJECT (type={type(data).__name__})"}
        lines = [f"ACTION: get_json_keys {file_path} — {len(data)} top-level keys", ""]
        for k, v in data.items():
            type_name = type(v).__name__
            if isinstance(v, str):
                preview = repr(v[:60])
            elif isinstance(v, (list, dict)):
                preview = f"[{len(v)} items]" if isinstance(v, list) else f"{{{len(v)} keys}}"
            else:
                preview = repr(v)
            lines.append(f"  {k!r}: {type_name} = {preview}")
        return {"success": True, "message": "\n".join(lines)}

    # ── Search & Listing ────────────────────────────────────────────────

    def _do_search(self, params: dict) -> dict:
        """Search file contents for a regex pattern using pure Python (cross-platform)."""
        pattern = params.get("pattern", "")
        search_path = params.get("path", ".")
        file_glob = params.get("glob", "*")
        max_matches = min(params.get("max_matches", 50), 200)

        if not pattern:
            return {"success": False, "message": "ACTION: search — ERROR: pattern is empty"}

        target = (self.project_dir / search_path).resolve()
        if not target.exists():
            return {"success": False, "message": f"ACTION: search — PATH NOT FOUND: {search_path}"}

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"success": False, "message": f"ACTION: search — INVALID REGEX: {exc}"}

        # Walk the directory tree and search file contents, grouped by file
        file_matches: Dict[str, List[str]] = {}
        search_root = target if target.is_dir() else target.parent
        total_matches = 0

        try:
            for filepath in search_root.rglob(file_glob if file_glob != "*" else "*"):
                if not filepath.is_file():
                    continue
                if filepath.suffix in ('.exe', '.dll', '.o', '.obj', '.lib', '.pdb', '.png', '.jpg', '.zip'):
                    continue
                if len(file_matches) >= 50:
                    break
                try:
                    text = filepath.read_text(encoding='utf-8', errors='ignore')
                    file_hits: List[str] = []
                    for line_num, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            file_hits.append(f"  {line_num}: {line.strip()}")
                            if total_matches + len(file_hits) >= max_matches:
                                break
                    if file_hits:
                        try:
                            rel = str(filepath.relative_to(self.project_dir)).replace("\\", "/")
                        except ValueError:
                            rel = str(filepath)
                        try:
                            mtime = datetime.fromtimestamp(filepath.stat().st_mtime).strftime('%Y-%m-%d')
                        except OSError:
                            mtime = '?'
                        file_matches[f"{rel} (modified: {mtime})"] = file_hits
                        total_matches += len(file_hits)
                except (OSError, UnicodeDecodeError):
                    continue
                if total_matches >= max_matches:
                    break
        except Exception as exc:
            return {"success": False, "message": f"ACTION: search — ERROR: {exc}"}

        truncated = total_matches >= max_matches

        result_lines: List[str] = []
        for file_header, hits in file_matches.items():
            result_lines.append(file_header)
            result_lines.extend(hits)

        results = "\n".join(result_lines)
        suffix = f" (showing {max_matches} of {total_matches}+)" if truncated else ""
        return {
            "success": True,
            "message": (
                f"ACTION: search pattern='{pattern}' — {total_matches} MATCHES{suffix}\n\n"
                f"--- RESULTS ---\n{results}\n--- END RESULTS ---"
            ),
        }

    def _do_list_files(self, params: dict) -> dict:
        """List files matching a glob pattern."""
        pattern = params.get("pattern", "**/*")
        base_path = params.get("path", ".")

        target = (self.project_dir / base_path).resolve()
        if not target.is_dir():
            return {"success": False, "message": f"ACTION: list_files — NOT A DIRECTORY: {base_path}"}

        try:
            matches = sorted(target.glob(pattern))
        except ValueError as exc:
            return {"success": False, "message": f"ACTION: list_files — INVALID PATTERN: {exc}"}

        # Filter to files only, make paths relative, add last_modified date
        files = []
        for m in matches:
            if m.is_file():
                try:
                    rel = str(m.relative_to(self.project_dir)).replace("\\", "/")
                except ValueError:
                    rel = str(m)
                try:
                    mtime = datetime.fromtimestamp(m.stat().st_mtime).strftime('%Y-%m-%d')
                except OSError:
                    mtime = '?'
                files.append(f"{rel} ({mtime})")
            if len(files) >= 500:
                break

        file_list = "\n".join(files)
        truncated = " (truncated at 500)" if len(files) >= 500 else ""
        return {
            "success": True,
            "message": (
                f"ACTION: list_files pattern='{pattern}' — {len(files)} FILES{truncated}\n\n"
                f"--- FILES ---\n{file_list}\n--- END FILES ---"
            ),
        }

    # ── Command Execution ───────────────────────────────────────────────

    def _do_run_command(self, params: dict) -> dict:
        """Run a shell command with safety checks.

        If the agent supplies a chained command (`A && B`, `A ; B`), we split
        on the chain operators and run each segment as its own subprocess.
        Each segment is independently safety-checked, so chained injection
        ("python; rm -rf /") still fails. shell=False is preserved everywhere.
        Reject other shell metas (pipe, backtick, $()) outright — those imply
        actual shell semantics we don't support without shell=True.
        """
        command = params.get("command", "")
        timeout = min(params.get("timeout", 120), 600)

        if not command:
            return {"success": False, "message": "ACTION: run_command — ERROR: command is empty"}

        # Reject shell metas that imply actual shell semantics. Chain operators
        # (&&, ;) are handled below by splitting the command into segments.
        for bad in ("||", "|", "$(", "`"):
            if bad in command:
                return {
                    "success": False,
                    "message": (
                        f"ACTION: run_command '{command}' — DENIED "
                        f"(shell operator '{bad}' is not supported; commands "
                        f"run with shell=False. Use && or ; to chain — those "
                        f"are split into separate subprocess calls — but "
                        f"pipes, redirects, command-substitution, and "
                        f"backticks are not available)"
                    ),
                }

        # Split on && / ; chain operators. Each segment is run as its own
        # subprocess (shell=False) so this stays safe.
        import re as _re
        segments = [s.strip() for s in _re.split(r"&&|;", command) if s.strip()]
        if not segments:
            return {"success": False, "message": "ACTION: run_command — ERROR: command is empty"}

        if len(segments) > 1:
            outputs = []
            last_rc = 0
            for seg in segments:
                # venv activator scripts (`.\venv\Scripts\activate`,
                # `source venv/bin/activate`) are no-ops under shell=False —
                # they only mutate the parent shell's environment, which we
                # don't have. Skip them with a note so the chain continues.
                if _re.search(
                    r"(?:^|[\\/])(activate(?:\.bat|\.fish|\.ps1)?|Activate\.ps1)\s*$",
                    seg, _re.IGNORECASE,
                ) or seg.lower().startswith("source "):
                    outputs.append(
                        f"--- segment: {seg} ---\n"
                        f"SKIPPED (activate is a no-op under shell=False; "
                        f"use `<venv>/Scripts/python.exe -m pip ...` directly)"
                    )
                    continue
                if not self._check_command(seg):
                    return {
                        "success": False,
                        "message": (
                            f"ACTION: run_command '{command}' — DENIED "
                            f"(segment '{seg}' is blocked or not in safe list)"
                        ),
                    }
                seg_result = self._do_run_command({"command": seg, "timeout": timeout})
                outputs.append(f"--- segment: {seg} ---\n{seg_result.get('message', '')}")
                if not seg_result.get("success", False):
                    last_rc = 1
                    break
            return {
                "success": last_rc == 0,
                "message": (
                    f"ACTION: run_command '{command}' — "
                    f"{'SUCCESS' if last_rc == 0 else 'FAIL'} "
                    f"(ran {len(outputs)}/{len(segments)} segment(s))\n\n"
                    + "\n\n".join(outputs)
                ),
            }

        # Single-segment path
        if not self._check_command(command):
            return {
                "success": False,
                "message": f"ACTION: run_command '{command}' — DENIED (blocked or not in safe list)",
            }

        try:
            # shell=False: prevents shell metacharacter injection
            cmd_tokens = shlex.split(command, posix=(os.name != 'nt'))
            proc = subprocess.run(
                cmd_tokens,
                capture_output=True, encoding='utf-8', errors='replace',
                timeout=timeout, cwd=str(self.project_dir),
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"ACTION: run_command '{command}' — TIMEOUT ({timeout}s)",
            }
        except OSError as exc:
            return {"success": False, "message": f"ACTION: run_command '{command}' — ERROR: {exc}"}

        output = (proc.stdout + proc.stderr).strip()
        # Cap output to avoid flooding context
        if len(output) > 20_000:
            output = output[:20_000] + "\n... (output truncated at 20000 chars)"

        status = "SUCCESS" if proc.returncode == 0 else "FAIL"
        return {
            "success": proc.returncode == 0,
            "message": (
                f"ACTION: run_command '{command}' — {status} (exit code {proc.returncode})\n\n"
                f"--- OUTPUT ---\n{output}\n--- END OUTPUT ---"
            ),
        }

    def _do_run_script(self, params: dict) -> dict:
        """Run a script from the scripts/ directory."""
        script = params.get("script", "")
        args = params.get("args", [])
        timeout = min(params.get("timeout", 120), 600)

        if not script:
            return {"success": False, "message": "ACTION: run_script — ERROR: script name is empty"}

        script_path = (self.project_dir / "scripts" / script).resolve()

        # Ensure script is within scripts/ directory
        scripts_dir = (self.project_dir / "scripts").resolve()
        if not str(script_path).startswith(str(scripts_dir)):
            return {
                "success": False,
                "message": f"ACTION: run_script '{script}' — DENIED (must be in scripts/ directory)",
            }

        if not script_path.is_file():
            return {
                "success": False,
                "message": f"ACTION: run_script '{script}' — FILE NOT FOUND in scripts/",
            }

        cmd = [str(script_path)] + (args if isinstance(args, list) else [str(args)])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, encoding='utf-8', errors='replace',
                timeout=timeout, cwd=str(self.project_dir),
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"ACTION: run_script '{script}' — TIMEOUT ({timeout}s)",
            }
        except OSError as exc:
            return {"success": False, "message": f"ACTION: run_script '{script}' — ERROR: {exc}"}

        output = (proc.stdout + proc.stderr).strip()
        if len(output) > 20_000:
            output = output[:20_000] + "\n... (output truncated at 20000 chars)"

        status = "SUCCESS" if proc.returncode == 0 else "FAIL"
        return {
            "success": proc.returncode == 0,
            "message": (
                f"ACTION: run_script '{script}' — {status} (exit code {proc.returncode})\n\n"
                f"--- OUTPUT ---\n{output}\n--- END OUTPUT ---"
            ),
        }

    # ── Git Operations ──────────────────────────────────────────────────

    def _do_commit(self, params: dict) -> dict:
        """Stage files and create a git commit."""
        files = params.get("files", [])
        message = params.get("message", "")

        if not message:
            return {"success": False, "message": "ACTION: commit — ERROR: commit message is empty"}

        if not files:
            return {"success": False, "message": "ACTION: commit — ERROR: no files specified"}

        if isinstance(files, str):
            files = [files]

        # Validate all paths before staging
        for f in files:
            check = self._check_path(f)
            if not check["safe"]:
                return {
                    "success": False,
                    "message": f"ACTION: commit — DENIED ({f}: {check['reason']})",
                }

        # Stage files
        add_cmd = ["git", "add", "--"] + files
        try:
            proc = subprocess.run(
                add_cmd, capture_output=True, encoding='utf-8', errors='replace',
                timeout=30, cwd=str(self.project_dir),
            )
            if proc.returncode != 0:
                return {
                    "success": False,
                    "message": f"ACTION: commit — GIT ADD FAILED: {proc.stderr.strip()}",
                }
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"success": False, "message": f"ACTION: commit — GIT ADD ERROR: {exc}"}

        # Commit
        commit_cmd = ["git", "commit", "-m", message]
        try:
            proc = subprocess.run(
                commit_cmd, capture_output=True, encoding='utf-8', errors='replace',
                timeout=30, cwd=str(self.project_dir),
            )
            if proc.returncode != 0:
                return {
                    "success": False,
                    "message": f"ACTION: commit — GIT COMMIT FAILED: {proc.stderr.strip()}",
                }
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"success": False, "message": f"ACTION: commit — GIT COMMIT ERROR: {exc}"}

        return {
            "success": True,
            "message": (
                f"ACTION: commit — SUCCESS "
                f"({len(files)} file{'s' if len(files) != 1 else ''} staged, "
                f"committed as '{message}')"
            ),
        }

    # ── Safety & Validation ─────────────────────────────────────────────

    @staticmethod
    def _diagnose_edit_mismatch(content: str, old_text: str, path: str) -> str:
        """Diagnose why old_text wasn't found — return a helpful hint."""
        hints = []

        # Check if it looks like concatenated key+value (missing quotes/colons/spaces)
        # e.g., "statusinactive" when file has '"status": "inactive"'
        stripped = old_text.replace('"', '').replace("'", '').replace(' ', '').replace(':', '')
        for line in content.splitlines():
            line_stripped = line.replace('"', '').replace("'", '').replace(' ', '').replace(':', '')
            if stripped and stripped in line_stripped:
                hints.append(
                    f"LIKELY MATCH: Your old_text '{old_text}' looks like a concatenated version of: '{line.strip()}'. "
                    f"You need to include the exact formatting (quotes, colons, spaces)."
                )
                break

        # Check whitespace mismatch
        if old_text.strip() != old_text and old_text.strip() in content:
            hints.append("WHITESPACE: old_text has leading/trailing whitespace. The trimmed version IS in the file.")
        elif old_text.strip() in content:
            hints.append(f"CLOSE MATCH: '{old_text.strip()}' exists in the file but not with this exact whitespace.")

        # Check case mismatch
        if old_text.lower() in content.lower() and old_text not in content:
            hints.append("CASE MISMATCH: The text exists with different capitalization.")

        # Check if old_text spans what looks like multiple lines flattened
        if '\n' not in old_text and len(old_text) > 30:
            # Might be multiple lines squashed together
            words = old_text.split()
            if len(words) >= 3:
                # Check if first and last words appear on different lines
                first_line = next((l for l in content.splitlines() if words[0] in l), None)
                last_line = next((l for l in content.splitlines() if words[-1] in l), None)
                if first_line and last_line and first_line != last_line:
                    hints.append("MULTI-LINE: old_text appears to span multiple lines that were flattened. Include \\n newlines.")

        # Show nearby content for context
        for i, line in enumerate(content.splitlines()):
            if any(word in line for word in old_text.split()[:2] if len(word) > 2):
                start = max(0, i - 1)
                end = min(len(content.splitlines()), i + 3)
                nearby = '\n'.join(f"  {n+1}: {l}" for n, l in enumerate(content.splitlines()[start:end], start))
                hints.append(f"NEARBY CONTENT (lines {start+1}-{end}):\n{nearby}")
                break

        return '\n'.join(hints) if hints else "No close matches found. Use read_file to see the exact file contents."

    # -----------------------------------------------------------------------
    # Config / Agent definition accessors (safe wrappers around project_settings)
    # -----------------------------------------------------------------------

    def _do_get_config(self, params: dict) -> dict:
        """Read a project setting by key from the project DB.
        Params: {"key": "setting_key"}
        """
        key = params.get("key", "")
        if not key:
            return {"success": False, "message": "ACTION: get_config — ERROR: 'key' param required"}
        if not (self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb):
            return {"success": False, "message": "ACTION: get_config — ERROR: no DB connection"}
        try:
            from aixyad_db import get_project_setting
            value = get_project_setting(self._db_handler.pdb, key, "")
            if not value:
                return {"success": True, "message": f"ACTION: get_config '{key}' — NOT SET (empty)"}
            # Truncate very large values
            preview = value[:2000] + ("…[truncated]" if len(value) > 2000 else "")
            return {"success": True, "message": f"ACTION: get_config '{key}':\n{preview}"}
        except Exception as exc:
            return {"success": False, "message": f"ACTION: get_config — ERROR: {exc}"}

    def _do_set_config(self, params: dict) -> dict:
        """Write a project setting by key into the project DB.
        Params: {"key": "setting_key", "value": "string or JSON string"}
        """
        key = params.get("key", "")
        value = params.get("value", "")
        if not key:
            return {"success": False, "message": "ACTION: set_config — ERROR: 'key' param required"}
        if not (self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb):
            return {"success": False, "message": "ACTION: set_config — ERROR: no DB connection"}
        try:
            from aixyad_db import set_project_setting
            set_project_setting(self._db_handler.pdb, key, str(value))
            return {"success": True, "message": f"ACTION: set_config '{key}' — SAVED ({len(str(value))} chars)"}
        except Exception as exc:
            return {"success": False, "message": f"ACTION: set_config — ERROR: {exc}"}

    def _do_get_agent_def(self, params: dict) -> dict:
        """Read an agent definition by slug.
        Checks project_settings first (key='agent_def_{slug}'), then falls back
        to the agents/ directory in the AIxYad install.
        Params: {"agent_slug": "developer"}
        """
        slug = params.get("agent_slug", "").lower().strip()
        if not slug:
            return {"success": False, "message": "ACTION: get_agent_def — ERROR: 'agent_slug' param required"}
        # Try project DB first
        if self._db_handler and hasattr(self._db_handler, 'pdb') and self._db_handler.pdb:
            try:
                from aixyad_db import get_project_setting
                stored = get_project_setting(self._db_handler.pdb, f"agent_def_{slug}", "")
                if stored:
                    preview = stored[:3000] + ("…[truncated]" if len(stored) > 3000 else "")
                    return {"success": True, "message": f"ACTION: get_agent_def '{slug}':\n{preview}"}
            except Exception:
                pass
        # Fall back to agents/ dir relative to executor install
        agents_dir = Path(__file__).resolve().parent / "agents"
        for name in [slug, slug.replace("-", "_"), slug.replace("_", "-")]:
            candidate = agents_dir / f"{name}.md"
            if candidate.is_file():
                content = candidate.read_text(encoding="utf-8", errors="replace")
                preview = content[:3000] + ("…[truncated]" if len(content) > 3000 else "")
                return {"success": True, "message": f"ACTION: get_agent_def '{slug}':\n{preview}"}
        return {"success": False, "message": f"ACTION: get_agent_def '{slug}' — NOT FOUND"}

    def _do_store_handoff(self, params: dict) -> dict:
        """Store handoff data for a task.
        Params: {task_id: str, files_changed: list, tests_written: list, 
                 coverage_notes: str, known_issues: str}
        """
        task_id = params.get("task_id", "")
        files_changed = params.get("files_changed", [])
        tests_written = params.get("tests_written", [])
        coverage_notes = params.get("coverage_notes", "")
        known_issues = params.get("known_issues", "")

        # Validation
        if not task_id:
            return {"success": False, "message": "ACTION: store_handoff — ERROR: task_id is required"}
        if not isinstance(files_changed, list):
            return {"success": False, "message": "ACTION: store_handoff — ERROR: files_changed must be a list"}
        if not isinstance(tests_written, list):
            return {"success": False, "message": "ACTION: store_handoff — ERROR: tests_written must be a list"}

        if not self._db_handler or not hasattr(self._db_handler, 'pdb') or not self._db_handler.pdb:
            return {"success": False, "message": "ACTION: store_handoff — ERROR: no DB connection"}

        # Build handoff JSON
        handoff_data = {
            "files_changed": files_changed,
            "tests_written": tests_written,
            "coverage_notes": coverage_notes,
            "known_issues": known_issues,
        }

        try:
            from aixyad_db import update_task
            success = update_task(self._db_handler.pdb, task_id, handoff_json=json.dumps(handoff_data))
            if not success:
                return {"success": False, "message": f"ACTION: store_handoff — ERROR: failed to update task {task_id}"}
            return {"success": True, "message": f"ACTION: store_handoff {task_id} — SUCCESS (handoff data stored)"}
        except Exception as exc:
            return {"success": False, "message": f"ACTION: store_handoff — ERROR: {exc}"}

    # Path components that are ALWAYS off-limits to the agent regardless of
    # permissions. These are system/tool dirs that an agent has no legitimate
    # reason to write into — touching them silently corrupts state (eg.
    # .git index) or breaks AIxYad's own bookkeeping (.aixyad DB).
    # Read-only access is also blocked because the agent shouldn't be
    # treating these as data sources either.
    HARD_FORBIDDEN_PATH_PARTS = frozenset({
        ".git",
        ".aixyad",       # per-project AIxYad state (DB, locks, sentinels)
        ".automatica",   # old name — still blocked until a project migrates
        ".venv", "venv", "env", "virtualenv",  # Python virtual envs
        "node_modules",
        "__pycache__",
        ".idea", ".vscode",
        ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".nox",
        "site-packages",
    })

    def _check_path(self, path: str) -> dict:
        """Verify a path is safe: inside project_dir, not in a hard-forbidden
        system directory, and not matching any user-configured blocked glob.

        ``HARD_FORBIDDEN_PATH_PARTS`` is non-overridable — even with
        ``allowed_paths`` set the agent cannot reach into ``.git`` /
        ``.aixyad`` / ``venv`` / etc. Those are tool/state directories
        and any 'edit' there is almost certainly a corruption."""
        try:
            resolved = (self.project_dir / path).resolve()
        except (ValueError, OSError):
            return {"safe": False, "reason": f"invalid path: {path}"}

        # Must be inside project directory
        try:
            rel = resolved.relative_to(self.project_dir)
        except ValueError:
            return {"safe": False, "reason": f"path escapes project directory: {path}"}

        # Hard-forbidden system directories (always blocked)
        for part in rel.parts:
            if part in self.HARD_FORBIDDEN_PATH_PARTS:
                return {
                    "safe": False,
                    "reason": (f"path traverses forbidden system directory "
                               f"'{part}/' — agent cannot read or write "
                               f"inside .git, .aixyad, venv, node_modules, "
                               f"__pycache__, IDE folders, or test caches"),
                }

        # Check blocked paths (glob patterns) from permissions
        blocked = self.permissions.get("blocked_paths", [])
        for pattern in blocked:
            if fnmatch.fnmatch(str(resolved), pattern) or fnmatch.fnmatch(path, pattern):
                return {"safe": False, "reason": f"path matches blocked pattern: {pattern}"}

        # Check allowed paths — if specified, path must match at least one
        allowed = self.permissions.get("allowed_paths", [])
        if allowed:
            if not any(
                fnmatch.fnmatch(str(resolved), a) or fnmatch.fnmatch(path, a)
                for a in allowed
            ):
                return {"safe": False, "reason": "path not in allowed_paths list"}

        return {"safe": True, "reason": ""}

    def _check_command(self, command: str) -> bool:
        """Check if a command is permitted to run."""
        cmd_lower = command.strip().lower()

        # Check VCS-specific permissions first (dangerous git operations)
        for vcs_cmd, perm_key in self.VCS_PERMISSION_MAP.items():
            if cmd_lower.startswith(vcs_cmd.lower()):
                default = self.VCS_PERMISSION_DEFAULTS.get(perm_key, False)
                if not self.permissions.get(perm_key, default):
                    return False
                return True  # Explicitly permitted VCS command

        # Check blocked commands (exact or prefix match)
        blocked = self.permissions.get("blocked_commands", [])
        for b in blocked:
            if cmd_lower.startswith(b.lower()):
                return False

        # Check explicitly allowed commands
        allowed = self.permissions.get("allowed_commands", [])
        for a in allowed:
            if cmd_lower.startswith(a.lower()):
                return True

        # Fall back to safe prefixes
        for prefix in self.SAFE_COMMAND_PREFIXES:
            if cmd_lower.startswith(prefix):
                return True

        return False

    # ── Logging ─────────────────────────────────────────────────────────

    def _log(
        self,
        timestamp: str,
        action: str,
        params: dict,
        result: dict,
        reasoning: str = "",
    ):
        """Record an action in the execution log."""
        entry = {
            "timestamp": timestamp,
            "action": action,
            "params": {k: v for k, v in params.items() if k != "content"},
            "success": result.get("success", False),
            "reasoning": reasoning,
        }
        # Store a truncated message for the log (full message is returned to caller)
        # Strip the "ACTION: X — " boilerplate prefix if present, keeping only the meaningful part
        msg = result.get("message", "")
        if " \u2014 " in msg:
            msg = msg.split(" \u2014 ", 1)[1]
        entry["summary"] = msg[:200] + "..." if len(msg) > 200 else msg
        self.action_log.append(entry)

    def get_log(self) -> List[Dict]:
        """Return the full action log."""
        return list(self.action_log)

    def get_log_summary(self) -> str:
        """Return a human-readable summary of all executed actions."""
        if not self.action_log:
            return "No actions executed."
        lines = []
        for entry in self.action_log:
            status = "OK" if entry["success"] else "FAIL"
            lines.append(f"[{entry['timestamp']}] {status} {entry['action']} — {entry['summary'][:80]}")
        return "\n".join(lines)

    # ── Self-Test ───────────────────────────────────────────────────────

    @classmethod
    def self_test(cls, tmp_dir: Optional[Path] = None) -> bool:
        """Validate all handlers work with a temporary project directory."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            scripts_dir = project / "scripts"
            scripts_dir.mkdir()

            permissions = {
                "can_read_file": True,
                "can_write_file": True,
                "can_edit_file": True,
                "can_search": True,
                "can_run_command": True,
                "can_commit": True,
                "blocked_paths": ["*.secret"],
                "allowed_commands": ["echo"],
                "blocked_commands": ["rm"],
            }

            executor = cls(project, permissions)
            errors = []

            # Test write_file
            r = executor.execute({"action": "write_file", "params": {"path": "test.txt", "content": "hello\nworld\n"}})
            if not r["success"]:
                errors.append(f"write_file: {r['message']}")

            # Test read_file
            r = executor.execute({"action": "read_file", "params": {"path": "test.txt"}})
            if not r["success"] or "hello" not in r["message"]:
                errors.append(f"read_file: {r['message']}")

            # Test edit_file
            r = executor.execute({"action": "edit_file", "params": {"path": "test.txt", "old_text": "hello", "new_text": "goodbye"}})
            if not r["success"]:
                errors.append(f"edit_file: {r['message']}")

            # Test list_files
            r = executor.execute({"action": "list_files", "params": {"pattern": "*.txt"}})
            if not r["success"]:
                errors.append(f"list_files: {r['message']}")

            # Test run_command
            r = executor.execute({"action": "run_command", "params": {"command": "echo test"}})
            if not r["success"]:
                errors.append(f"run_command: {r['message']}")

            # Test blocked command
            r = executor.execute({"action": "run_command", "params": {"command": "rm -rf /"}})
            if r["success"]:
                errors.append("run_command should have blocked 'rm'")

            # Test path escape
            r = executor.execute({"action": "read_file", "params": {"path": "../../etc/passwd"}})
            if r["success"]:
                errors.append("read_file should have blocked path escape")

            # Test blocked path pattern
            r = executor.execute({"action": "write_file", "params": {"path": "data.secret", "content": "x"}})
            if r["success"]:
                errors.append("write_file should have blocked .secret path")

            # Test permission denial
            restricted = cls(project, {"can_write_file": False})
            r = restricted.execute({"action": "write_file", "params": {"path": "nope.txt", "content": "x"}})
            if r["success"]:
                errors.append("write_file should have been denied by permission")

            # Test step_done (always allowed)
            r = executor.execute({"action": "step_done", "params": {}})
            if not r["success"]:
                errors.append(f"step_done: {r['message']}")

            # Test unknown action
            r = executor.execute({"action": "fly_to_moon", "params": {}})
            if r["success"]:
                errors.append("unknown action should have failed")

            if errors:
                print(f"SELF-TEST FAILED ({len(errors)} errors):")
                for e in errors:
                    print(f"  - {e}")
                return False

            print(f"SELF-TEST PASSED — {len(executor.get_log())} actions logged")
            return True


if __name__ == "__main__":
    AgentExecutor.self_test()
