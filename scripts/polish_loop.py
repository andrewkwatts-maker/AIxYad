#!/usr/bin/env python3
"""
polish_loop.py -- Automatica continuous improvement daemon.

Runs all day, every INTERVAL minutes.  Each cycle:
  1. Zips both repos as timestamped backups (keeps last MAX_BACKUPS per repo)
  2. Reads POLISH_TODO.md, picks the next pending [ ] item
  3. Registers gemma3:4b provider if not present
  4. Adds the task to the target repo DB and runs a 1-sprint pass
  5. Marks item [x] done or [!] failed, logs results
  6. Sleeps INTERVAL minutes, then repeats

Usage:
  python scripts/polish_loop.py                  # full daemon (30-min loop)
  python scripts/polish_loop.py --interval 15    # 15-min loop
  python scripts/polish_loop.py --once           # single iteration, no sleep
  python scripts/polish_loop.py --backup-only    # backup and exit
  python scripts/polish_loop.py --list           # show pending TODO items

Stop at any time with Ctrl+C.
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

_SCRIPT_DIR  = Path(__file__).resolve().parent
ENGINE_DIR   = _SCRIPT_DIR.parent                       # H:/Github/Automatica
UI_DIR       = ENGINE_DIR.parent / "Automatica-App"    # H:/Github/Automatica-App
BACKUP_DIR   = ENGINE_DIR.parent / "Automatica-Backups"
TODO_FILE    = ENGINE_DIR / "POLISH_TODO.md"
LOG_DIR      = ENGINE_DIR / "logs"
RUN_AUTO     = ENGINE_DIR / "run_autonomous.py"

# ── Defaults ───────────────────────────────────────────────────────────────────

INTERVAL_MIN  = 30        # minutes between cycles
MAX_BACKUPS   = 10        # keep last N zips per repo
# PROVIDER must match a sprint_runner adapter name (Ollama, claude, etc.)
# The DB display name (registered via --AddProvider) is PROVIDER_DISPLAY_NAME
PROVIDER              = "Ollama"        # adapter type recognised by sprint_runner
PROVIDER_DISPLAY_NAME = "Gemma3-4b"    # DB display name for --AddProvider
MODEL         = "gemma3:4b"            # 4B: responds with code, num_predict=4096 in settings
OLLAMA_URL    = "http://localhost:11434"
SPRINT_MINS   = 30        # max minutes for a single sprint execution

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

# Ensure stdout handles UTF-8 (sprint runner output can contain non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_today   = datetime.datetime.now().strftime("%Y%m%d")
_logfile = LOG_DIR / f"polish_loop_{_today}.log"

_stream_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        _stream_handler,
        logging.FileHandler(str(_logfile), encoding="utf-8"),
    ],
)
log = logging.getLogger("polish_loop")


# ── Backup ────────────────────────────────────────────────────────────────────

def backup_repos() -> None:
    """Zip ENGINE and UI repos with a timestamp, prune old backups."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    repos = [
        ("Automatica",     ENGINE_DIR),
        ("Automatica-App", UI_DIR),
    ]

    for name, path in repos:
        if not path.exists():
            log.warning("Repo not found, skipping backup: %s", path)
            continue
        out_stem = BACKUP_DIR / f"{name}_{ts}"
        try:
            shutil.make_archive(
                str(out_stem), "zip",
                root_dir=str(path.parent),
                base_dir=path.name,
            )
            size_mb = (out_stem.with_suffix(".zip")).stat().st_size / 1_048_576
            log.info("Backed up %-20s -> %s.zip  (%.1f MB)", name, out_stem.name, size_mb)
        except Exception as exc:
            log.error("Backup failed for %s: %s", name, exc)
            continue

        # Prune old backups: keep newest MAX_BACKUPS
        existing = sorted(BACKUP_DIR.glob(f"{name}_*.zip"))
        for old in existing[:-MAX_BACKUPS]:
            try:
                old.unlink()
                log.debug("Pruned old backup: %s", old.name)
            except Exception:
                pass


# ── TODO parsing ──────────────────────────────────────────────────────────────

_TODO_RE = re.compile(
    r"^- \[(?P<status>[ x~!-])\]\s+(?P<target>ENGINE|UI)\s*\|\s*(?P<priority>HIGH|MEDIUM|LOW)\s*\|\s*(?P<desc>.+)$"
)


def _read_todo_lines() -> list[str]:
    if not TODO_FILE.exists():
        log.error("TODO file not found: %s", TODO_FILE)
        return []
    return TODO_FILE.read_text(encoding="utf-8").splitlines(keepends=True)


def get_pending_items() -> list[dict]:
    """Return all pending ([ ]) items from POLISH_TODO.md."""
    items = []
    for idx, line in enumerate(_read_todo_lines()):
        m = _TODO_RE.match(line.rstrip("\n"))
        if m and m.group("status") == " ":
            items.append({
                "line_idx":  idx,
                "target":    m.group("target"),
                "priority":  m.group("priority"),
                "desc":      m.group("desc").strip(),
                "raw":       line,
            })
    return items


def get_next_item() -> dict | None:
    """Pick the highest-priority pending item (HIGH > MEDIUM > LOW)."""
    items = get_pending_items()
    if not items:
        return None
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return min(items, key=lambda x: rank.get(x["priority"], 9))


def mark_item(line_idx: int, status: str) -> None:
    """Replace [ ] with [x]/[~]/[!]/[-] at the given line index in-place."""
    lines = _read_todo_lines()
    if line_idx >= len(lines):
        return
    old = lines[line_idx]
    new = re.sub(r"^(- \[)[ ](])", rf"\g<1>{status}\2", old)
    if new == old:
        # Already updated (e.g. by parallel process) -- try replacing any marker
        new = re.sub(r"^(- \[)[x~! -](])", rf"\g<1>{status}\2", old)
    lines[line_idx] = new
    TODO_FILE.write_text("".join(lines), encoding="utf-8")


# ── Provider registration ─────────────────────────────────────────────────────

def _ensure_provider() -> bool:
    """Register gemma3:4b in the global DB if not already present."""
    try:
        result = subprocess.run(
            [sys.executable, str(RUN_AUTO), "--ListProviders"],
            capture_output=True, text=True, timeout=15,
            cwd=str(ENGINE_DIR),
        )
        if PROVIDER_DISPLAY_NAME.lower() in result.stdout.lower():
            log.debug("Provider %s already registered.", PROVIDER_DISPLAY_NAME)
            return True
        # Not registered -- add it
        r = subprocess.run(
            [sys.executable, str(RUN_AUTO),
             "--AddProvider",    PROVIDER_DISPLAY_NAME,
             "--ProviderType",   "http_local_chat",
             "--ProviderUrl",    OLLAMA_URL,
             "--ProviderModel",  MODEL],
            capture_output=True, text=True, timeout=15,
            cwd=str(ENGINE_DIR),
        )
        if r.returncode == 0:
            log.info("Registered provider: %s (%s)", PROVIDER_DISPLAY_NAME, MODEL)
            return True
        log.warning("Provider registration failed: %s", r.stderr[:200])
        return False
    except Exception as exc:
        log.warning("Could not ensure provider: %s", exc)
        return False


# ── Ollama health check ───────────────────────────────────────────────────────

def _ollama_running() -> bool:
    """Return True only if Ollama is running AND the required model is installed."""
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5)
        data = json.loads(req.read())
        models = [m.get("name", "") for m in data.get("models", [])]
        if not any(MODEL.split(":")[0] in m for m in models):
            log.warning("Ollama running but %s not installed -- skipping sprint.", MODEL)
            log.warning("Install it with: ollama pull %s", MODEL)
            return False
        return True
    except Exception:
        return False


# ── Task execution ────────────────────────────────────────────────────────────

def _repo_for(target: str) -> Path:
    return ENGINE_DIR if target == "ENGINE" else UI_DIR


_PY_MODULE_RE = re.compile(r"\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\.(py|py\b)", re.ASCII)
_KNOWN_FILES_RE = re.compile(
    r"\b(sprint_runner|run_autonomous|agent_protocol|agent_executor|"
    r"automatica_db|project_scanner|ai_adapters|ollama_utils|"
    r"validation|db_actions|view_actions|vision_actions|"
    r"api_server|app|workspace|nav_rail|nav_data|tab_bar|soft_card|section_list|"
    r"settings|management|overview|logs|agents|standards|pipeline|"
    r"project_detail|setup_wizard|projects)\b",
    re.ASCII,
)


def _link_files_to_task(repo: Path, task_display_id: str, desc: str) -> None:
    """
    Parse desc for Python module/file mentions, upsert them into project_files,
    and link to the task via entity_files so the sprint runner provides context.
    """
    import sqlite3 as _sqlite3

    db_path = repo / ".automatica" / "project.db"
    if not db_path.exists():
        return

    # Collect candidate filenames from description
    candidates: list[str] = []
    for m in _KNOWN_FILES_RE.finditer(desc):
        candidates.append(m.group(1) + ".py")
    # Also catch explicit "foo.py" mentions
    for m in re.finditer(r"\b(\w+\.py)\b", desc):
        candidates.append(m.group(1))

    if not candidates:
        return

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row

        task_row = conn.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)
        ).fetchone()
        if not task_row:
            conn.close()
            return
        task_id = task_row["id"]

        code_snippets: list[str] = []
        linked = []
        seen = set()
        for fname in candidates:
            if fname in seen:
                continue
            seen.add(fname)
            # Find actual path on disk
            matches = list(repo.rglob(fname))
            if not matches:
                continue
            file_abs = matches[0]
            rel_path = str(file_abs.relative_to(repo)).replace("\\", "/")

            # Upsert into project_files
            conn.execute(
                "INSERT INTO project_files (path) VALUES (?) ON CONFLICT(path) DO NOTHING",
                (rel_path,)
            )
            file_row = conn.execute(
                "SELECT id FROM project_files WHERE path = ?", (rel_path,)
            ).fetchone()
            if not file_row:
                continue

            # Link task → file (ignore if already linked)
            conn.execute("""
                INSERT INTO entity_files (entity_type, entity_id, file_id, relationship, added_by)
                VALUES ('task', ?, ?, 'related', 'polish_loop')
                ON CONFLICT DO NOTHING
            """, (task_id, file_row["id"]))
            linked.append(rel_path)

            # Extract relevant code section: find any mentioned function/method in desc
            # Matches .method_name and ._private_method (captures full name with underscore)
            func_names = re.findall(r"\.(_?[a-zA-Z_]\w+)\b", desc)
            try:
                file_lines = file_abs.read_text(encoding="utf-8", errors="replace").splitlines()
                for fn in func_names[:3]:  # limit to first 3 candidates
                    for i, line in enumerate(file_lines):
                        if re.search(rf"\bdef {fn}\b", line):
                            snippet_lines = file_lines[i:i + 35]
                            snippet = "\n".join(snippet_lines)
                            code_snippets.append(
                                f"\n-- {rel_path}:{i+1}, def {fn} --\n{snippet}\n-- end --"
                            )
                            break
            except Exception:
                pass

        # Inject code context into the task description so the agent sees actual code
        if code_snippets:
            combined = desc + "\n\nEXISTING CODE FOR REFERENCE:" + "".join(code_snippets)
            conn.execute(
                "UPDATE tasks SET description = ? WHERE id = ?",
                (combined, task_id)
            )

        conn.commit()
        conn.close()
        if linked:
            log.info("  Linked files: %s", ", ".join(linked))
        if code_snippets:
            log.info("  Injected %d code snippet(s) into task description", len(code_snippets))
    except Exception as exc:
        log.debug("  _link_files_to_task error: %s", exc)


def _try_direct_create(repo: Path, desc: str) -> bool:
    """
    Fast path for 'Create path/to/file.ext: description' tasks.
    Calls Ollama /api/generate directly, extracts code, writes the file.
    Bypasses the sprint runner entirely -- no agent protocol needed.
    Returns True if the file was successfully written.
    """
    m = re.match(r"Create\s+([\w/\\]+\.\w+)\s*:\s*(.+)", desc, re.DOTALL | re.IGNORECASE)
    if not m:
        return False

    rel_path = m.group(1).replace("\\", "/")
    file_desc = m.group(2).strip()
    target_path = repo / rel_path

    ext  = Path(rel_path).suffix.lstrip(".")
    lang = {"py": "Python", "md": "Markdown", "yaml": "YAML", "json": "JSON",
            "txt": "text", "sh": "shell"}.get(ext, "text")

    prompt = (
        f"Write the complete {lang} source code for the following file.\n\n"
        f"File path: {rel_path}\n"
        f"Requirements: {file_desc}\n\n"
        f"Rules:\n"
        f"- Output ONLY the raw file content. No explanation, no preamble.\n"
        f"- Do NOT wrap in markdown code fences (no ```python blocks).\n"
        f"- The very first character of your response must be the first character of the file.\n"
        f"- Include all necessary imports at the top.\n"
        f"- Write complete, working code — no placeholders or TODOs.\n"
    )

    log.info("  Direct create: calling Ollama for %s ...", rel_path)
    try:
        import urllib.request as _ureq
        import json as _json

        payload = _json.dumps({
            "model":  MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 4096, "temperature": 0.15},
        }).encode()
        req = _ureq.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=180) as resp:
            data = _json.loads(resp.read())
        content = data.get("response", "").strip()
    except Exception as exc:
        log.warning("  Direct Ollama call failed: %s", exc)
        return False

    if not content or len(content) < 20:
        log.warning("  Empty / too-short response (%d chars) -- skipping", len(content))
        return False

    # Strip markdown fences if model ignored the instructions
    content = re.sub(r"^```[\w]*\r?\n?", "", content)
    content = re.sub(r"\r?\n?```\s*$", "", content)
    content = content.strip() + "\n"

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    log.info("  Created %s  (%d bytes)", rel_path, len(content))
    return True


def run_task(item: dict) -> bool:
    """
    Add the TODO item as a task to the appropriate repo DB and run a 1-sprint pass.
    Returns True if sprint reports at least 1 task completed.
    """
    repo  = _repo_for(item["target"])
    desc  = item["desc"]
    label = f"[{item['target']}|{item['priority']}]"

    if not repo.exists():
        log.error("%s Repo not found: %s", label, repo)
        return False

    # ── Fast path: direct file creation (bypasses sprint runner for Create tasks) ──
    if re.match(r"Create\s+[\w/\\]+\.\w+\s*:", desc, re.IGNORECASE):
        log.info("%s Attempting direct file creation (no sprint runner)...", label)
        if _try_direct_create(repo, desc):
            return True
        log.info("%s Direct creation failed -- falling back to sprint runner", label)

    # ── Step 1: Add task to backlog ────────────────────────────────────────────
    log.info("%s Adding task: %s", label, desc[:80])
    r = subprocess.run(
        [sys.executable, str(RUN_AUTO),
         "--Project", str(repo),
         "--AddTask",  desc],
        capture_output=True, text=True, timeout=30,
        cwd=str(ENGINE_DIR),
    )
    if r.returncode != 0:
        log.error("%s --AddTask failed: %s", label, r.stderr[:300])
        return False

    # Extract task display_id from stdout ("TASK-001: ..." format)
    task_id_match = re.search(r"(TASK-\d+)", r.stdout)
    if task_id_match:
        _link_files_to_task(repo, task_id_match.group(1), desc)

    log.debug("%s Task added: %s", label, r.stdout.strip()[:100])

    # ── Step 2: Run 1 sprint ───────────────────────────────────────────────────
    log.info("%s Starting sprint (max %d min)...", label, SPRINT_MINS)
    r = subprocess.run(
        [sys.executable, str(RUN_AUTO),
         "--Project",       str(repo),
         "--StartSprint",
         "--provider",      PROVIDER,
         "--Model",         MODEL,
         "--max-minutes",   str(SPRINT_MINS),
         "--max-sprints",   "1",
         "--max-iterations","10"],
        capture_output=True, text=True,
        timeout=SPRINT_MINS * 60 + 30,
        cwd=str(ENGINE_DIR),
    )

    stdout = r.stdout or ""
    stderr = r.stderr or ""

    # Log a compact summary
    for line in (stdout + stderr).splitlines():
        if any(k in line for k in ("[DONE]", "[FAIL]", "[SKIP]", "[SPRINT]",
                                    "completed", "failed", "ERROR", "error")):
            log.info("  %s", line.strip()[:120])

    if r.returncode != 0:
        log.error("%s Sprint exited non-zero (%d): %s", label, r.returncode, stderr[:200])
        return False

    # Consider success only if sprint explicitly reports tasks done (not just the word "done")
    completed = (
        "[DONE]" in stdout or
        "tasks_completed: " in stdout or
        "status: done" in stdout.lower() or
        re.search(r"\b[1-9]\d*/\d+ completed\b", stdout) is not None or
        re.search(r"done\s*\|\s*[1-9]", stdout) is not None
    )
    log.info("%s Sprint finished -- %s", label, "SUCCESS" if completed else "no tasks completed")
    return completed


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle() -> None:
    """Execute one full polish cycle: backup -> pick item -> run -> mark."""
    now = datetime.datetime.now().strftime("%H:%M:%S")
    log.info("=" * 60)
    log.info("CYCLE START  %s", now)
    log.info("=" * 60)

    # 1. Backup
    log.info("Step 1/4  Backing up repos...")
    backup_repos()

    # 2. Ensure provider registered
    log.info("Step 2/4  Checking provider registration...")
    _ensure_provider()

    # 3. Check Ollama health
    if not _ollama_running():
        log.warning("Step 3/4  Skipping execution -- Ollama not ready (see above).")
    else:
        log.info("Step 3/4  Ollama online [OK]  model=%s", MODEL)

        # 4. Pick next TODO
        item = get_next_item()
        if item is None:
            log.info("Step 4/4  No pending TODO items -- all done for today! [all done!]")
        else:
            log.info("Step 4/4  Next item: [%s|%s] %s",
                     item["target"], item["priority"], item["desc"][:80])
            mark_item(item["line_idx"], "~")  # mark in-progress

            try:
                success = run_task(item)
                mark_item(item["line_idx"], "x" if success else "!")
                status = "[OK] done" if success else "[X] failed"
                log.info("Item %s: %s", status, item["desc"][:80])
            except KeyboardInterrupt:
                mark_item(item["line_idx"], " ")  # reset on Ctrl+C
                raise
            except Exception as exc:
                log.error("run_task raised: %s", exc)
                mark_item(item["line_idx"], "!")

    # Summary
    pending = len(get_pending_items())
    log.info("CYCLE END  -- %d items still pending in POLISH_TODO.md", pending)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Automatica continuous-improvement daemon")
    parser.add_argument("--interval", type=int, default=INTERVAL_MIN,
                        help=f"Minutes between cycles (default: {INTERVAL_MIN})")
    parser.add_argument("--once",        action="store_true", help="Run one cycle then exit")
    parser.add_argument("--backup-only", action="store_true", help="Backup only, then exit")
    parser.add_argument("--list",        action="store_true", help="List pending TODO items")
    args = parser.parse_args()

    if args.list:
        # Force UTF-8 output so emoji/arrows don't crash on Windows cp1252 console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
        items = get_pending_items()
        if not items:
            print("No pending TODO items.")
        else:
            print(f"{len(items)} pending items:")
            for i in items:
                desc = i['desc'][:80].encode("ascii", errors="replace").decode("ascii")
                print(f"  [{i['target']}|{i['priority']}] {desc}")
        return

    if args.backup_only:
        backup_repos()
        return

    log.info("Polish loop starting -- interval=%d min, log=%s", args.interval, _logfile)
    log.info("ENGINE=%s", ENGINE_DIR)
    log.info("UI    =%s", UI_DIR)
    log.info("MODEL =%s @ %s", MODEL, OLLAMA_URL)

    if args.once:
        run_cycle()
        return

    # Continuous loop
    cycle = 0
    while True:
        cycle += 1
        log.info("--- Cycle #%d ---", cycle)
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Interrupted by user -- stopping polish loop.")
            break
        except Exception as exc:
            log.error("Cycle #%d raised unexpected error: %s", cycle, exc)
            # Continue -- don't let one bad cycle kill the daemon

        # Re-check at start of new day -- rotate log file
        new_today = datetime.datetime.now().strftime("%Y%m%d")
        if new_today != _today:
            new_logfile = LOG_DIR / f"polish_loop_{new_today}.log"
            fh = logging.FileHandler(str(new_logfile), encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                              datefmt="%H:%M:%S"))
            logging.getLogger().addHandler(fh)
            log.info("New day -- logging to %s", new_logfile)

        pending = len(get_pending_items())
        if pending == 0:
            log.info("All TODO items completed -- daemon will keep running but skip execution.")

        log.info("Sleeping %d minutes until next cycle...  (Ctrl+C to stop)\n", args.interval)
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            log.info("Interrupted during sleep -- stopping.")
            break


if __name__ == "__main__":
    main()
