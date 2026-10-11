#!/usr/bin/env python3
"""
claude_polish_loop.py — Autonomous improvement daemon powered by Claude API.

Picks items from POLISH_TODO.md every INTERVAL minutes, reads the relevant source
files, calls Claude with tool use (read/write/edit/search), and applies the changes.

Usage:
    python scripts/claude_polish_loop.py                    # 30-min loop
    python scripts/claude_polish_loop.py --once             # single item, then exit
    python scripts/claude_polish_loop.py --interval 60      # hourly
    python scripts/claude_polish_loop.py --list             # show pending items
    python scripts/claude_polish_loop.py --item 5           # run specific item by index

Requirements:
    pip install anthropic
    ANTHROPIC_API_KEY must be set in environment (or .env in repo root)
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_DIR  = _SCRIPT_DIR.parent
UI_DIR      = ENGINE_DIR.parent / "Automatica-App"
BACKUP_DIR  = ENGINE_DIR.parent / "AIxYad-Backups"
TODO_FILE   = ENGINE_DIR / "POLISH_TODO.md"
LOG_DIR     = ENGINE_DIR / "logs"

# ── Config ─────────────────────────────────────────────────────────────────────

INTERVAL_MIN = 30
MAX_BACKUPS  = 10
MODEL        = "claude-sonnet-4-6"
MAX_TOKENS   = 8192
MAX_TOOL_ROUNDS = 30   # max back-and-forth tool-use rounds per task

# ── Logging ────────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_today   = datetime.datetime.now().strftime("%Y%m%d")
_logfile = LOG_DIR / f"claude_polish_{_today}.log"
_file_handler = logging.FileHandler(str(_logfile), encoding="utf-8")

# C-18: install SecretFilter on file handler to scrub secrets before writing
try:
    sys.path.insert(0, str(ENGINE_DIR))
    from log_filter import SecretFilter as _SF
    _file_handler.addFilter(_SF())
    logging.getLogger().addFilter(_SF())
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _file_handler,
    ],
)
log = logging.getLogger("claude_polish")


# ── .env loader ───────────────────────────────────────────────────────────────

def _load_env():
    """Load ANTHROPIC_API_KEY from .env if not already in environment."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    env_file = ENGINE_DIR / ".env"
    if not env_file.exists():
        env_file = ENGINE_DIR.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["ANTHROPIC_API_KEY"] = key
                return


# ── Backup ────────────────────────────────────────────────────────────────────

def _make_filtered_archive(output_path: str, source_dir: Path) -> None:
    """Create a zip archive of source_dir, skipping .git and build artefacts."""
    SKIP = {'.git', '__pycache__', '.pytest_cache', 'aixyad.egg-info', 'automatica.egg-info', 'backups'}
    with zipfile.ZipFile(output_path + '.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in source_dir.rglob('*'):
            if any(part in SKIP for part in p.parts):
                continue
            if p.is_file():
                zf.write(p, p.relative_to(source_dir.parent))


def backup_repos() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for name, path in [("AIxYad", ENGINE_DIR), ("Automatica-App", UI_DIR)]:
        if not path.exists():
            continue
        out_stem = BACKUP_DIR / f"{name}_{ts}"
        try:
            _make_filtered_archive(str(out_stem), path)
            size_mb = (out_stem.with_suffix(".zip")).stat().st_size / 1_048_576
            log.info("Backed up %-20s -> %s.zip  (%.1f MB)", name, out_stem.name, size_mb)
        except Exception as exc:
            log.error("Backup failed for %s: %s", name, exc)
        # Prune old
        for old in sorted(BACKUP_DIR.glob(f"{name}_*.zip"))[:-MAX_BACKUPS]:
            try:
                old.unlink()
            except Exception:
                pass


# ── TODO parsing ──────────────────────────────────────────────────────────────

_TODO_RE = re.compile(
    r"^- \[(?P<status>[ x~!-])\]\s+(?P<target>ENGINE|UI)\s*\|\s*"
    r"(?P<priority>HIGH|MEDIUM|LOW)\s*\|\s*(?P<desc>.+)$"
)


def _read_lines() -> list[str]:
    return TODO_FILE.read_text(encoding="utf-8").splitlines(keepends=True)


def get_pending_items() -> list[dict]:
    items = []
    for idx, line in enumerate(_read_lines()):
        m = _TODO_RE.match(line.rstrip("\n"))
        if m and m.group("status") == " ":
            items.append({"line_idx": idx, "target": m.group("target"),
                          "priority": m.group("priority"), "desc": m.group("desc").strip()})
    return items


def get_next_item() -> dict | None:
    items = get_pending_items()
    if not items:
        return None
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return min(items, key=lambda x: rank.get(x["priority"], 9))


def mark_item(line_idx: int, status: str) -> None:
    lines = _read_lines()
    if line_idx >= len(lines):
        return
    old = lines[line_idx]
    new = re.sub(r"^(- \[)[x~! -](])", rf"\g<1>{status}\2", old)
    if new == old:
        new = re.sub(r"^(- \[) (])", rf"\g<1>{status}\2", old)
    lines[line_idx] = new
    TODO_FILE.write_text("".join(lines), encoding="utf-8")


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Path is relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "start_line": {"type": "integer", "description": "First line to read (1-based, optional)"},
                "end_line":   {"type": "integer", "description": "Last line to read (1-based, optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write complete content to a file (creates or overwrites).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Complete file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file with new content. "
            "old_str must match exactly (including indentation/whitespace). "
            "Use read_file first to get the exact current content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path relative to repo root"},
                "old_str": {"type": "string", "description": "Exact string to find and replace"},
                "new_str": {"type": "string", "description": "Replacement string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a directory matching an optional glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Directory path relative to repo root (default: .)"},
                "pattern": {"type": "string", "description": "Glob pattern (default: **/*.py)"},
            },
        },
    },
    {
        "name": "search_in_files",
        "description": "Search for a regex pattern across files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path":    {"type": "string", "description": "Directory or file to search (default: .)"},
                "glob":    {"type": "string", "description": "Glob filter, e.g. '*.py'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "task_done",
        "description": "Signal that the task is complete. Provide a summary of what was changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief description of what was implemented"},
                "files_changed": {"type": "array", "items": {"type": "string"},
                                  "description": "List of files that were modified or created"},
            },
            "required": ["summary"],
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def _repo_for(target: str) -> Path:
    return ENGINE_DIR if target == "ENGINE" else UI_DIR


def execute_tool(name: str, inputs: dict, repo: Path) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if name == "read_file":
            p = repo / inputs["path"]
            if not p.exists():
                return f"ERROR: File not found: {inputs['path']}"
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, (inputs.get("start_line") or 1) - 1)
            end   = inputs.get("end_line")
            if end:
                lines = lines[start:end]
            else:
                lines = lines[start:]
            numbered = "\n".join(f"{start+i+1:4}: {l}" for i, l in enumerate(lines))
            return numbered or "(empty file)"

        elif name == "write_file":
            p = repo / inputs["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(inputs["content"], encoding="utf-8")
            return f"OK: wrote {inputs['path']}  ({len(inputs['content'])} chars)"

        elif name == "edit_file":
            p = repo / inputs["path"]
            if not p.exists():
                return f"ERROR: File not found: {inputs['path']}"
            original = p.read_text(encoding="utf-8")
            old_str = inputs["old_str"]
            new_str = inputs["new_str"]
            if old_str not in original:
                # Show surrounding context for diagnosis
                preview = original[:500].replace("\n", "\\n")
                return (
                    f"ERROR: old_str not found in {inputs['path']}.\n"
                    f"File starts with: {preview}\n"
                    f"Make sure you copied the exact text including indentation."
                )
            count = original.count(old_str)
            if count > 1:
                return (
                    f"ERROR: old_str appears {count} times — provide more context to make it unique."
                )
            updated = original.replace(old_str, new_str, 1)
            p.write_text(updated, encoding="utf-8")
            return f"OK: edited {inputs['path']}"

        elif name == "list_files":
            base    = repo / (inputs.get("path") or ".")
            pattern = inputs.get("pattern") or "**/*.py"
            matches = sorted(base.rglob(pattern.lstrip("**/")))
            rel = [str(m.relative_to(repo)).replace("\\", "/") for m in matches
                   if m.is_file() and ".aixyad" not in m.parts and ".automatica" not in m.parts
                   and "__pycache__" not in m.parts]
            if not rel:
                return "(no files matched)"
            return "\n".join(rel[:200])

        elif name == "search_in_files":
            import subprocess as _sp
            base = repo / (inputs.get("path") or ".")
            glob = inputs.get("glob") or "*.py"
            pat  = inputs["pattern"]
            result = _sp.run(
                ["python", "-c",
                 f"import re, pathlib\n"
                 f"base=pathlib.Path(r'{base}')\n"
                 f"matches=[]\n"
                 f"for f in base.rglob('{glob}'):\n"
                 f"  if '__pycache__' in f.parts: continue\n"
                 f"  try:\n"
                 f"    txt=f.read_text(encoding='utf-8',errors='replace').splitlines()\n"
                 f"    for i,l in enumerate(txt,1):\n"
                 f"      if re.search({repr(pat)},l):\n"
                 f"        rel=str(f.relative_to(base)).replace(chr(92),'/')\n"
                 f"        matches.append(f'{{rel}}:{{i}}: {{l.rstrip()}}')\n"
                 f"  except: pass\n"
                 f"print(chr(10).join(matches[:100]))"],
                capture_output=True, text=True, timeout=30
            )
            return result.stdout or result.stderr or "(no matches)"

        elif name == "task_done":
            # Sentinel — handled by caller
            return "TASK_DONE"

        else:
            return f"ERROR: Unknown tool: {name}"

    except Exception as exc:
        return f"ERROR in {name}: {exc}"


# ── Claude agentic loop ───────────────────────────────────────────────────────

def _gather_relevant_files(repo: Path, desc: str) -> dict[str, str]:
    """Read files mentioned by name in the task description."""
    _known = re.compile(
        r'\b(sprint_runner|run_autonomous|agent_protocol|agent_executor|'
        r'aixyad_db|project_scanner|validation|db_actions|view_actions|'
        r'api_server|app|workspace|nav_rail|nav_data|tab_bar|soft_card|'
        r'section_list|settings|management|overview|logs|agents|standards|'
        r'pipeline|project_detail|setup_wizard|projects|'
        r'adapters[/\\]__init__|adapters[/\\]_base|adapters[/\\]http_local_chat)'
        r'\.py\b|\b(\w+)\.py\b',
        re.IGNORECASE,
    )
    candidates: set[str] = set()
    for m in _known.finditer(desc):
        name = (m.group(1) or m.group(2) or "").replace("\\", "/")
        if name:
            candidates.add(name if name.endswith(".py") else name + ".py")

    contents: dict[str, str] = {}
    for fname in list(candidates)[:6]:  # cap at 6 files
        matches = list(repo.rglob(Path(fname).name))
        if matches:
            try:
                text = matches[0].read_text(encoding="utf-8", errors="replace")
                rel = str(matches[0].relative_to(repo)).replace("\\", "/")
                # Truncate very large files to ~400 lines
                lines = text.splitlines()
                if len(lines) > 400:
                    text = "\n".join(lines[:400]) + f"\n... ({len(lines)-400} more lines)"
                contents[rel] = text
            except Exception:
                pass
    return contents


def run_task_with_claude(item: dict) -> tuple[bool, str]:
    """
    Run one TODO item by calling `claude --print` as a subprocess.
    Asks Claude to emit structured <write_file> / <edit_file> blocks,
    then parses and applies them.
    Returns (success, summary).
    """
    repo  = _repo_for(item["target"])
    desc  = item["desc"]
    label = f"[{item['target']}|{item['priority']}]"

    # Gather relevant file contents to include in the prompt
    file_contents = _gather_relevant_files(repo, desc)
    files_section = ""
    if file_contents:
        parts = []
        for path, content in file_contents.items():
            parts.append(f"=== {path} ===\n{content}\n=== end {path} ===")
        files_section = "\n\nRELEVANT SOURCE FILES:\n" + "\n\n".join(parts)

    prompt = f"""You are implementing a code improvement task on the AIxYad project.

REPO ROOT: {repo}

TASK:
{desc}
{files_section}

OUTPUT INSTRUCTIONS:
Implement the task above. Output your changes using these XML tags:

To create or overwrite a file:
<write_file path="relative/path/to/file.py">
file content here
</write_file>

To edit an existing file (replace exact text):
<edit_file path="relative/path/to/file.py">
<old>exact existing text to replace (must match perfectly including indentation)</old>
<new>replacement text</new>
</edit_file>

Rules:
- Use <edit_file> for changes to existing files — include enough context (2-3 surrounding lines) to make old text unique
- Use <write_file> only for new files
- Make minimal focused changes — only what the task requires
- After all changes, write one line: DONE: <brief summary of what you implemented>
- If you cannot complete it, write: BLOCKED: <reason>
- Do not include any other explanation outside the XML tags and the DONE/BLOCKED line
"""

    # Resolve claude CLI — use shutil.which first, then fallback locations
    _CLAUDE_FALLBACKS = [
        r"C:\Users\Andrew\AppData\Roaming\npm\claude.cmd",
        str(Path.home() / "AppData/Roaming/npm/claude.cmd"),
        str(Path.home() / "AppData/Roaming/npm/claude"),
        "/usr/local/bin/claude",
    ]
    _claude_exe = shutil.which("claude") or shutil.which("claude.cmd")
    if _claude_exe is None:
        for _c in _CLAUDE_FALLBACKS:
            if Path(_c).exists():
                _claude_exe = _c
                break
    if _claude_exe is None:
        return False, "claude CLI not found — tried PATH and common npm locations"

    log.info("%s  Calling claude --print (exe=%s) ...", label, _claude_exe)
    try:
        result = subprocess.run(
            [_claude_exe, "--print", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True, text=True,
            timeout=300, cwd=str(repo),
            encoding="utf-8", errors="replace",
            shell=False,
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0 and not output:
            err = (result.stderr or "")[:400]
            return False, f"claude CLI error (exit {result.returncode}): {err}"
    except FileNotFoundError:
        return False, f"claude CLI not found at {_claude_exe}"
    except subprocess.TimeoutExpired:
        return False, "claude CLI timed out after 300s"
    except Exception as exc:
        return False, f"subprocess error: {exc}"

    if not output:
        return False, "Empty response from claude"

    log.debug("%s  Raw output (%d chars):\n%s", label, len(output), output[:500])

    # Parse and apply <write_file> blocks
    files_changed: list[str] = []
    write_re = re.compile(
        r'<write_file\s+path=["\']?([^"\'>\s]+)["\']?\s*>(.*?)</write_file>',
        re.DOTALL,
    )
    for m in write_re.finditer(output):
        rel_path, content = m.group(1).strip(), m.group(2)
        # Strip leading newline that comes right after the tag
        content = content.lstrip("\n")
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        log.info("%s  write_file: %s (%d bytes)", label, rel_path, len(content))
        files_changed.append(rel_path)

    # Parse and apply <edit_file> blocks
    edit_re = re.compile(
        r'<edit_file\s+path=["\']?([^"\'>\s]+)["\']?\s*>\s*'
        r'<old>(.*?)</old>\s*<new>(.*?)</new>\s*</edit_file>',
        re.DOTALL,
    )
    for m in edit_re.finditer(output):
        rel_path, old_text, new_text = m.group(1).strip(), m.group(2), m.group(3)
        target = repo / rel_path
        if not target.exists():
            log.warning("%s  edit_file: %s NOT FOUND", label, rel_path)
            continue
        original = target.read_text(encoding="utf-8")
        if old_text not in original:
            log.warning("%s  edit_file: old text not found in %s", label, rel_path)
            continue
        updated = original.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        log.info("%s  edit_file: %s", label, rel_path)
        files_changed.append(rel_path)

    # Extract DONE / BLOCKED line
    summary = ""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("DONE:") or line.startswith("BLOCKED:"):
            summary = line
            break

    if not summary and not files_changed:
        # No structured output at all — treat as failure
        return False, f"No changes applied. Output: {output[:200]}"

    if not summary:
        summary = f"DONE: {len(files_changed)} file(s) changed"

    blocked = summary.startswith("BLOCKED:")
    success = not blocked and bool(files_changed or summary.startswith("DONE:"))
    log.info("%s  %s", label, summary[:120])
    return success, summary


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle() -> None:
    now = datetime.datetime.now().strftime("%H:%M:%S")
    log.info("=" * 60)
    log.info("CYCLE START  %s  (model=%s)", now, MODEL)
    log.info("=" * 60)

    log.info("Step 1/3  Backing up repos...")
    backup_repos()

    item = get_next_item()
    if item is None:
        log.info("Step 2/3  No pending TODO items — all done!")
        return

    log.info("Step 2/3  Next item: [%s|%s] %s",
             item["target"], item["priority"], item["desc"][:80])
    mark_item(item["line_idx"], "~")

    try:
        log.info("Step 3/3  Running Claude agent loop...")
        success, summary = run_task_with_claude(item)
        mark_item(item["line_idx"], "x" if success else "!")
        status = "[OK] done" if success else "[X] failed"
        log.info("Item %s: %s", status, item["desc"][:80])
        if summary:
            log.info("  Summary: %s", summary[:200])
    except KeyboardInterrupt:
        mark_item(item["line_idx"], " ")
        raise
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
        mark_item(item["line_idx"], "!")

    pending = len(get_pending_items())
    log.info("CYCLE END  -- %d items still pending in POLISH_TODO.md", pending)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global MODEL
    _load_env()

    parser = argparse.ArgumentParser(description="Claude-powered AIxYad polish daemon")
    parser.add_argument("--interval", type=int, default=INTERVAL_MIN,
                        help=f"Minutes between cycles (default: {INTERVAL_MIN})")
    parser.add_argument("--once",   action="store_true", help="Run one cycle then exit")
    parser.add_argument("--list",   action="store_true", help="List pending TODO items")
    parser.add_argument("--item",   type=int, default=None,
                        help="Run specific item by 0-based index in pending list")
    parser.add_argument("--model",  default=None, help=f"Claude model (default: {MODEL})")
    args = parser.parse_args()

    if args.model:
        MODEL = args.model

    if args.list:
        items = get_pending_items()
        print(f"{len(items)} pending items:")
        for i, it in enumerate(items):
            print(f"  [{i}] [{it['target']}|{it['priority']}] {it['desc'][:90]}")
        return

    if args.item is not None:
        items = get_pending_items()
        if args.item >= len(items):
            print(f"ERROR: item index {args.item} out of range (0-{len(items)-1})")
            sys.exit(1)
        item = items[args.item]
        log.info("Running specific item [%d]: %s", args.item, item["desc"][:80])
        mark_item(item["line_idx"], "~")
        success, summary = run_task_with_claude(item)
        mark_item(item["line_idx"], "x" if success else "!")
        log.info("Item %s: %s", "[OK]" if success else "[X]", summary[:200])
        return

    log.info("Claude polish loop starting -- interval=%d min, model=%s", args.interval, MODEL)
    log.info("ENGINE=%s", ENGINE_DIR)
    log.info("UI    =%s", UI_DIR)
    log.info("Log   =%s", _logfile)

    if args.once:
        run_cycle()
        return

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error("Cycle error: %s", exc)

        log.info("Sleeping %d minutes...", args.interval)
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
