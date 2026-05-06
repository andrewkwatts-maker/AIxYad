#!/usr/bin/env python3
"""
Autonomous multi-sprint runner — runs sprints continuously until all backlog tasks are
done or the time limit is reached.

Usage:
    python run_autonomous.py --project-dir H:/Github/Pong2 --provider Ollama \
        --max-minutes 15 --max-sprints 10

Observations are written to logs/autonomous_<project>.log
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

def load_backlog_counts(project_dir: Path):
    import sqlite3
    db = project_dir / ".automatica" / "project.db"
    if not db.exists():
        return {"backlog": 0, "sprint_planned": 0, "in_progress": 0, "done": 0, "failed": 0, "blocked": 0, "total": 0, "has_goal": False}
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    # Check if a project_goal is set (tasks may be auto-generated on first sprint)
    goal_row = conn.execute(
        "SELECT value FROM project_settings WHERE key IN ('project_goal','global_goal') AND value != '' LIMIT 1"
    ).fetchone()
    # Also check pipeline_config.project_goal
    if not goal_row:
        cfg_row = conn.execute("SELECT value FROM project_settings WHERE key='pipeline_config'").fetchone()
        if cfg_row:
            try:
                cfg = json.loads(cfg_row[0])
                goal_row = cfg.get("project_goal", "")
            except Exception:
                pass
    conn.close()
    counts = dict(rows)
    total = sum(counts.values())
    return {
        "backlog": counts.get("backlog", 0),
        "sprint_planned": counts.get("sprint_planned", 0),
        "in_progress": counts.get("in_progress", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "blocked": counts.get("blocked", 0),
        "total": total,
        "has_goal": bool(goal_row),
    }

def _get_project_conn(project_dir: Path):
    """Open project DB, initialising it if needed. Returns sqlite3.Connection."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from automatica_db import init_project_db
    project_dir.mkdir(parents=True, exist_ok=True)
    return init_project_db(project_dir)


def _get_global_conn():
    """Open the global automatica DB. Returns sqlite3.Connection."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from automatica_db import init_global_db
    db_path = Path.home() / ".automatica" / "automatica.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return init_global_db(db_path)


def cmd_add_provider(name: str, provider_type: str, url: str = '',
                     model: str = '', key_env: str = '', max_tokens: int = 4096):
    """Add an AI provider to the global DB."""
    if not provider_type:
        print("[ADD PROVIDER] --ProviderType is required. "
              "e.g. http_chat_completions, http_local_chat, http_generate_content, subprocess_cli")
        return
    from automatica_db import add_provider
    conn = _get_global_conn()
    result = add_provider(conn, name=name, provider_type=provider_type,
                          base_url=url, model_name=model, api_key_env=key_env,
                          max_tokens=max_tokens)
    conn.close()
    print(f"[ADD PROVIDER] id={result['id']}  name={result['name']}  type={provider_type}")
    if key_env:
        print(f"  → Export {key_env} before running sprints.")


def cmd_list_providers():
    """List all configured AI providers."""
    from automatica_db import get_providers
    conn = _get_global_conn()
    providers = get_providers(conn)
    conn.close()
    if not providers:
        print("[PROVIDERS] None configured. Use --AddProvider to add one.")
        return
    print(f"[PROVIDERS] {len(providers)} configured:")
    for p in providers:
        status = "enabled" if p.get("enabled", 1) else "disabled"
        print(f"  [{p['id']}] {p['name']}  type={p.get('provider_type','')}  "
              f"model={p.get('model_name','')}  url={p.get('base_url','')}  "
              f"key_env={p.get('api_key_env','')}  ({status})")


def cmd_remove_provider(name_or_id: str):
    """Remove an AI provider by name or ID."""
    from automatica_db import get_providers, remove_provider
    conn = _get_global_conn()
    providers = get_providers(conn)
    target = None
    for p in providers:
        if str(p['id']) == name_or_id or p['name'].lower() == name_or_id.lower():
            target = p
            break
    if not target:
        print(f"[REMOVE PROVIDER] No provider found matching '{name_or_id}'")
        conn.close()
        return
    remove_provider(conn, target['id'])
    conn.close()
    print(f"[REMOVE PROVIDER] Removed '{target['name']}' (id={target['id']})")


def cmd_add_task(project_dir: Path, task_text: str, claim_hours: float = 1.0):
    """Add a task to the project backlog.

    WF-3: by default, sets manual_claim_until = now + claim_hours so the
    autonomous loop won't race against humans / parallel agents working on
    the same task. Pass claim_hours=0 to skip the claim window.
    """
    from datetime import datetime, timedelta, timezone
    conn = _get_project_conn(project_dir)
    max_id = conn.execute(
        "SELECT MAX(CAST(SUBSTR(display_id,6) AS INT)) FROM tasks WHERE display_id LIKE 'TASK-%'"
    ).fetchone()[0] or 0
    display_id = f"TASK-{max_id + 1:03d}"
    claim_until = None
    if claim_hours > 0:
        claim_until = (datetime.now(timezone.utc) + timedelta(hours=claim_hours)
                       ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO tasks (display_id, title, priority, complexity, description, status, manual_claim_until) "
        "VALUES (?,?,?,?,?,?,?)",
        (display_id, task_text[:200], "MEDIUM", 3, task_text, "backlog", claim_until)
    )
    conn.commit()
    claim_msg = f" [claimed until {claim_until}]" if claim_until else ""
    print(f"[ADD TASK] {display_id}: {task_text[:80]}{claim_msg}")
    conn.close()


def cmd_add_sprint(project_dir: Path, sprint_goal: str, pkg_dir: Path, provider: str):
    """Create a new sprint with the given goal (planning only, no execution)."""
    proc = subprocess.run(
        [sys.executable, "-u", str(_pkg_dir / "sprint_runner.py"),
         "--project-dir", str(project_dir), "--provider", provider,
         "--dry-run", "--goal", sprint_goal],
        capture_output=True, text=True, cwd=str(_pkg_dir)
    )
    print(proc.stdout or proc.stderr)


def cmd_order_backlog(project_dir: Path, pkg_dir: Path, provider: str):
    """Use AI to reprioritize and order the backlog."""
    import sys as _sys
    _sys.path.insert(0, str(_pkg_dir))
    from sprint_runner import SprintRunner, load_settings, get_provider_config
    settings = load_settings(_pkg_dir / "settings.json")
    provider_config = get_provider_config(settings, provider)
    runner = SprintRunner(project_dir=project_dir, provider=provider, provider_config=provider_config,
                          max_parallel=1, max_iterations_per_task=6)
    conn = runner._get_main_project_conn()
    tasks = conn.execute("SELECT display_id, title, priority, complexity FROM tasks WHERE status='backlog' ORDER BY id").fetchall()
    if not tasks:
        print("[ORDER] No backlog tasks to order")
        return
    print(f"[ORDER] Ordering {len(tasks)} backlog tasks by priority and dependency...")
    # Simple rule-based reorder: HIGH before MEDIUM before LOW; lower complexity first within same priority
    priority_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_tasks = sorted(tasks, key=lambda t: (priority_rank.get(t[2], 1), t[3]))
    for i, task in enumerate(sorted_tasks):
        conn.execute("UPDATE tasks SET sort_order=? WHERE display_id=?", (i, task[0]))
    conn.commit()
    print("[ORDER] Backlog reordered:")
    for t in sorted_tasks:
        print(f"  {t[0]} [{t[2]}] {t[1][:60]}")
    conn.close()


def cmd_sprint_review(project_dir: Path):
    """Show a summary of the last sprint's results."""
    conn = _get_project_conn(project_dir)
    sprint = conn.execute("SELECT id, sprint_number, goal, status FROM sprints ORDER BY sprint_number DESC LIMIT 1").fetchone()
    if not sprint:
        print("[REVIEW] No sprints found")
        return
    sprint_id, sprint_number, goal, status = sprint
    tasks = conn.execute(
        "SELECT t.display_id, t.title, st.status FROM tasks t "
        "JOIN sprint_tasks st ON t.id = st.task_id WHERE st.sprint_id=?", (sprint_id,)
    ).fetchall()
    done = sum(1 for t in tasks if t[2] == "done")
    print(f"[REVIEW] Sprint #{sprint_number}: {status}")
    print(f"  Goal: {goal or '(none)'}")
    print(f"  Tasks: {done}/{len(tasks)} completed")
    for t in tasks:
        print(f"    {t[0]} [{t[2]}] {t[1][:60]}")
    conn.close()


def _detect_language(goal: str) -> str:
    """Heuristic language pick from the project goal text. Maps common
    keywords to one of the standards/<lang>.md files. Defaults to python
    because that's the most common autonomous-target language."""
    g = (goal or "").lower()
    if any(k in g for k in ("c++", "cpp", "cmake ", "engine", "vulkan", ".hpp", ".cpp")):
        return "cpp"
    if any(k in g for k in ("c#", "csharp", "dotnet", ".net ", "unity", "razor")):
        return "csharp"
    if any(k in g for k in (" go ", "golang", "go module")):
        return "go"
    if any(k in g for k in ("rust ", "cargo", ".rs", "rustc")):
        return "rust"
    if any(k in g for k in ("java ", "spring boot", "maven", "gradle")):
        return "java"
    if any(k in g for k in ("typescript", "react", " tsx", " ts ", "next.js", "vite")):
        return "typescript"
    return "python"


def _generate_claude_md(project_dir: Path, goal: str, language: str,
                         pkg_dir: Path) -> bool:
    """Generate a CLAUDE.md (project standards) for a freshly-initialised
    project. Combines the language-specific standards template at
    standards/<lang>.md with the user-supplied goal + a generic SOLID/DRY
    boilerplate. Skips silently if a CLAUDE.md (or PROJECT_STANDARDS.md /
    STANDARDS.md) already exists.

    Returns True if a new file was written, False if one already existed
    or the generator hit an error."""
    for existing in ("CLAUDE.md", "PROJECT_STANDARDS.md", "STANDARDS.md"):
        if (project_dir / existing).is_file():
            return False  # don't overwrite a hand-curated standards doc

    template_path = pkg_dir / "standards" / f"{language}.md"
    template_text = ""
    if template_path.is_file():
        try:
            template_text = template_path.read_text(encoding="utf-8",
                                                      errors="replace")
        except Exception:
            template_text = ""

    # Per-language layout sketch. Drives the ALLOWED PATHS list so the
    # agent gets concrete file-path bans up front (top of CLAUDE.md, before
    # any 3K truncation can hide them). Falls back to a sensible Python
    # layout when the language has no entry.
    layout_by_lang = {
        "python": [
            "src/__init__.py", "src/main.py",
            "src/<module>.py        # one file per concept",
            "tests/test_<module>.py",
            "config/<name>.yaml",
            "build.bat              # pyinstaller invocation",
            "requirements.txt",
            "README.md", "STANDARDS.md (or CLAUDE.md)",
        ],
        "cpp": [
            "src/<module>.cpp", "include/<module>.hpp",
            "tests/test_<module>.cpp",
            "CMakeLists.txt",
            "config/<name>.yaml",
            "README.md", "STANDARDS.md",
        ],
        "typescript": [
            "src/<module>.ts", "src/index.ts",
            "tests/<module>.test.ts",
            "package.json", "tsconfig.json",
            "README.md", "STANDARDS.md",
        ],
        "rust": [
            "src/lib.rs", "src/main.rs", "src/<module>.rs",
            "tests/<module>_test.rs",
            "Cargo.toml",
            "README.md", "STANDARDS.md",
        ],
    }
    paths = layout_by_lang.get(language, layout_by_lang["python"])

    layout_block = (
        "## ALLOWED PATHS — every file you write MUST fit this layout\n\n"
        "```\n"
        f"{project_dir.name}/\n"
        + "\n".join(f"├── {p}" for p in paths)
        + "\n```\n\n"
        "EXPLICITLY BANNED at the project root (write_file these and the\n"
        "executor will warn you next turn):\n"
        f"- `{project_dir.name.lower()}.py`  ← entry point lives in src/main.py, NEVER at root\n"
        f"- `game.py`, `paddle.py`, `ball.py` at root  ← these go in src/\n"
        f"- duplicate basenames (e.g. `paddle.py` AND `subdir/paddle.py`)\n"
        f"- parallel folders for one concept: `paddle/`, `paddle_ai/`, `pong/`, `game/`\n"
        f"- `*_ai/`, `*_helper/`, `*_utils/` sibling folders\n"
        f"- inventing files the task description didn't name\n\n"
        "When a task says REFACTOR or EXTRACT, you MUST `edit_file` an\n"
        "existing path from the list above — never write to a NEW path\n"
        "unless the task names that exact path explicitly.\n"
    )

    refactor_rule = (
        "## Refactor rule (HARD)\n\n"
        "If a task title or description starts with REFACTOR / EXTRACT / "
        "RENAME / SPLIT / MOVE / RESTRUCTURE / INLINE / MERGE / OPTIMISE / "
        "POLISH / ADD DOCSTRINGS, you MUST `edit_file` the existing target "
        "in place. You may add a new class WITHIN the same file, but DO "
        "NOT create a new sibling file unless the task description names "
        "a new path explicitly. The executor will warn you on basename "
        "collisions across folders — treat that warning as a near-failure "
        "and `discard_file` the new write.\n"
    )

    quality_block = (
        "## Quality gates\n\n"
        "- Tests pass before any task closes (`pytest tests/` for Python).\n"
        "- Type hints / annotations on public APIs.\n"
        "- Docstrings on classes + entry points (indexer auto-fills missing).\n"
        "- User-facing errors handled with clear messages — no silent crashes.\n"
        "- ≥90% line coverage on code paths the user invokes.\n"
    )

    common_block = (
        "## Architectural principles\n\n"
        "Apply SOLID rigorously:\n"
        "- **Single Responsibility**: each module/class has ONE reason to change.\n"
        "- **Open/Closed**: extend by composition, not by editing shipped classes.\n"
        "- **Liskov / ISP / DIP**: small interfaces, depend on abstractions.\n\n"
        "**DRY**: extract a helper when logic repeats. Don't copy-paste.\n\n"
        "**Separation of concerns**: rendering / input / business logic / "
        "configuration / persistence each live in their own module.\n\n"
        "**Configuration**: prefer YAML for human-tweakable config (supports "
        "comments + hierarchy). JSON only when a tool requires it.\n"
    )

    # ORDER MATTERS — the prompt-builder caps standards text at 6000 chars,
    # so the rules that ban duplicate files MUST be in the first ~3000.
    # Layout block, refactor rule, then quality block, then SOLID, finally
    # the language template (which often runs 5-7 KB and goes last).
    body = (
        f"# Project Standards — {project_dir.name}\n\n"
        f"_Auto-generated by Automatica from `standards/{language}.md` + "
        f"the project goal. Hand-edit this file to override; the runtime "
        f"won't replace it once it exists._\n\n"
        f"## Goal\n\n{goal.strip() or '(no goal set)'}\n\n"
        + layout_block + "\n"
        + refactor_rule + "\n"
        + quality_block + "\n"
        + common_block + "\n"
        f"## Language standards (from standards/{language}.md)\n\n"
        + (template_text if template_text else
           f"_(no language template found at standards/{language}.md — "
           f"falling back to language-agnostic SOLID rules above)_")
        + "\n"
    )

    out_path = project_dir / "CLAUDE.md"
    try:
        out_path.write_text(body, encoding="utf-8")
        print(f"[INIT] Generated CLAUDE.md ({language} template, {len(body)}B)")
        return True
    except Exception as exc:
        print(f"[INIT] Could not write CLAUDE.md: {exc}")
        return False


def _init_project_if_needed(project_dir: Path, goal: str, provider: str, model: str):
    """Initialize project DB with goal and default pipeline if not already set up."""
    import sys as _sys
    _pkg_dir = Path(__file__).resolve().parent
    _sys.path.insert(0, str(_pkg_dir))
    from automatica_db import init_project_db, set_project_setting, set_project_setting_json, get_project_setting

    project_dir.mkdir(parents=True, exist_ok=True)
    conn = init_project_db(project_dir)

    existing_goal = get_project_setting(conn, "project_goal", "")
    if goal and not existing_goal:
        set_project_setting(conn, "project_goal", goal)
        print(f"[INIT] Project goal set: {goal[:80]}")

    # Generate CLAUDE.md from the goal + language-specific standards template
    # if one isn't already present. Surfaces in every agent prompt as
    # "== PROJECT STANDARDS ==" and feeds the replenish review prompt too.
    _lang = _detect_language(goal or existing_goal)
    _generate_claude_md(project_dir, goal or existing_goal, _lang, _pkg_dir)

    # Set default pipeline if none exists
    from automatica_db import get_project_setting_json
    existing_pipeline = get_project_setting_json(conn, "pipeline_config")
    if not existing_pipeline:
        pipeline_config = {
            "max_parallel": 1,
            "max_points_per_sprint": 40,
            "max_iterations_per_task": 12,
            "provider": provider,
            "model": model,
            "steps": [
                {"name": "Backlog Refinement", "phase_type": "builtin", "step_order": 1, "enabled": True, "config_json": {}},
                {"name": "Sprint Planning",    "phase_type": "builtin", "step_order": 2, "enabled": True, "config_json": {}},
                {"name": "Task Execution",     "phase_type": "builtin", "step_order": 3, "enabled": True, "config_json": {}},
                {"name": "Sprint Review",      "phase_type": "review",  "step_order": 4, "enabled": True, "config_json": {}},
            ]
        }
        set_project_setting_json(conn, "pipeline_config", pipeline_config)
        conn.execute("DELETE FROM pipeline_steps")
        for step in pipeline_config["steps"]:
            conn.execute(
                "INSERT INTO pipeline_steps (name, phase_type, step_order, enabled, config_json) VALUES (?,?,?,?,?)",
                (step["name"], step["phase_type"], step["step_order"], 1, json.dumps(step["config_json"]))
            )
        conn.commit()
        print(f"[INIT] Default pipeline config created")

    conn.close()


def _wake_up_scan(project_dir: Path, provider: str, log_fn=print) -> dict:
    """On launch, walk the project tree and re-summarise any file whose
    content has changed since the indexer last touched it.

    The user may have hand-edited files between Automatica runs (or a
    sibling tool like the IDE may have made changes). Without this scan,
    the project standards / module docstrings / folder summaries / README
    auto-section all drift out of date until the agent happens to edit the
    same file again.

    Cheap: SummaryIndexer.summarise_python_file's first action is an
    embedded-hash comparison against the file's current non-comment content.
    Files whose @automatica-summary hash matches current short-circuit
    without ever calling Ollama. Only genuinely-changed files trigger a
    summary refresh.

    Returns a dict like {"files": N, "folders": N, "project": N}.
    """
    try:
        import sys as _sys
        _pkg_dir = Path(__file__).resolve().parent
        if str(_pkg_dir) not in _sys.path:
            _sys.path.insert(0, str(_pkg_dir))
        import sqlite3 as _sq3
        from agent_protocol import get_provider_config
        from summary_indexer import SummaryIndexer

        # Pull the right Ollama endpoint + summary model from the global DB
        # so the wake-up scan uses the same model as the rest of the loop.
        _global_db_path = Path.home() / ".automatica" / "automatica.db"
        _endpoint = "http://localhost:11434"
        _model = "llama3.2:3b"
        try:
            gdb = _sq3.connect(str(_global_db_path))
            gdb.row_factory = _sq3.Row
            cfg = get_provider_config(gdb, provider) or {}
            _endpoint = cfg.get("endpoint", _endpoint)
            _model = cfg.get("summary_model", "llama3.2:3b")
            gdb.close()
        except Exception:
            pass

        indexer = SummaryIndexer(
            project_dir=project_dir, endpoint=_endpoint, model=_model,
        )
        log_fn("  [WAKEUP] scanning project for hand-edited / out-of-date files…")
        counts = indexer.full_walk(force=False)
        total = counts["files"] + counts["folders"] + counts["project"]
        if total > 0:
            log_fn(f"  [WAKEUP] refreshed {counts['files']} file / "
                   f"{counts['folders']} folder / {counts['project']} project "
                   f"summary block(s) — hand-edits detected, summaries now fresh")
        else:
            log_fn(f"  [WAKEUP] no changes since last run; summaries already current")
        return counts
    except Exception as exc:
        log_fn(f"  [WAKEUP] scan failed: {exc}")
        return {"files": 0, "folders": 0, "project": 0}


def _replenish_via_review(project_dir: Path, provider: str, log_fn=print) -> int:
    """Mid-run backlog replenishment. Constructs a SprintRunner just long
    enough to call replenish_backlog_from_review, then closes it. Decoupled
    from the sprint subprocess so we don't spin up sprint_runner.py just to
    add tasks. Returns the number of tasks added."""
    try:
        import sys as _sys
        _pkg_dir = Path(__file__).resolve().parent
        if str(_pkg_dir) not in _sys.path:
            _sys.path.insert(0, str(_pkg_dir))
        import sqlite3 as _sq3
        from sprint_runner import SprintRunner
        # Resolve provider config from the global DB (same path as the
        # sprint subprocess uses).
        _global_db_path = Path.home() / ".automatica" / "automatica.db"
        gdb = _sq3.connect(str(_global_db_path))
        gdb.row_factory = _sq3.Row
        try:
            from agent_protocol import get_provider_config
            provider_config = get_provider_config(gdb, provider) or {}
        finally:
            gdb.close()
        # Plug provider_type from DB row into config (SprintRunner expects it).
        runner = SprintRunner(
            project_dir=project_dir,
            provider=provider,
            provider_config=provider_config,
            max_parallel=1,
            log_dir=_pkg_dir / "logs" / "sprints",
        )
        pconn = _sq3.connect(str(project_dir / ".automatica" / "project.db"))
        pconn.row_factory = _sq3.Row
        try:
            added = runner.replenish_backlog_from_review(pconn)
        finally:
            pconn.close()
        return int(added or 0)
    except Exception as exc:
        log_fn(f"  [REPLENISH] Failed: {exc}")
        return 0


def _bump_stalled_tasks(project_dir: Path, log_fn=print) -> None:
    """Increment complexity by one Fibonacci step for all blocked/failed tasks.

    Called after 3 consecutive zero-velocity sprints to escape stuck states.
    Fibonacci steps: 1→2→3→5→8→13→21 (capped at 21).
    """
    _fib = [1, 2, 3, 5, 8, 13, 21]

    def _next_fib(n: int) -> int:
        for f in _fib:
            if f > n:
                return f
        return _fib[-1]  # already at max

    import sqlite3 as _sq3
    db_path = project_dir / ".automatica" / "project.db"
    if not db_path.exists():
        return
    conn = _sq3.connect(str(db_path))
    conn.row_factory = _sq3.Row
    rows = conn.execute(
        "SELECT display_id, complexity FROM tasks WHERE status IN ('blocked', 'failed', 'backlog')"
    ).fetchall()
    bumped = 0
    for row in rows:
        new_c = _next_fib(row["complexity"] or 3)
        if new_c != row["complexity"]:
            conn.execute(
                "UPDATE tasks SET complexity=?, status='backlog' WHERE display_id=?",
                (new_c, row["display_id"])
            )
            bumped += 1
    conn.commit()
    conn.close()
    log_fn(f"  [HEAL] Bumped complexity on {bumped} stalled task(s) and reset to backlog")


def _auto_estimate_complexity(project_dir: Path, log_fn=print) -> None:
    """S3-W10: Auto-estimate complexity for backlog tasks that have the default value (3).

    Scoring:
      - >50 words in description     → +1
      - Each file mention beyond 2   → +1
      - Keywords: refactor, redesign, integrate → +2 each
    Cap at 8. Updates DB if estimate differs from current value.
    """
    import re as _re
    import sqlite3 as _sq3

    db_path = project_dir / ".automatica" / "project.db"
    if not db_path.exists():
        return

    _fib_valid = {1, 2, 3, 5, 8, 13, 21, 34, 55, 89}

    def _nearest_fib(n: int) -> int:
        for f in sorted(_fib_valid):
            if f >= n:
                return f
        return 8  # cap

    try:
        conn = _sq3.connect(str(db_path))
        conn.row_factory = _sq3.Row
        rows = conn.execute(
            "SELECT display_id, description, complexity FROM tasks "
            "WHERE status = 'backlog' AND complexity = 3"
        ).fetchall()

        updated = 0
        for row in rows:
            desc = row["description"] or ""
            score = 3  # baseline

            # Word count
            words = desc.split()
            if len(words) > 50:
                score += 1

            # File mentions (tokens containing a dot followed by extension chars)
            file_mentions = _re.findall(r'\b[\w/\\.-]+\.\w{1,6}\b', desc)
            extra_files = max(0, len(file_mentions) - 2)
            score += extra_files

            # Keyword bumps
            desc_lower = desc.lower()
            for kw in ("refactor", "redesign", "integrate"):
                if kw in desc_lower:
                    score += 2

            # Cap and snap to Fibonacci
            score = min(score, 8)
            new_complexity = _nearest_fib(score)

            if new_complexity != row["complexity"]:
                conn.execute(
                    "UPDATE tasks SET complexity = ? WHERE display_id = ?",
                    (new_complexity, row["display_id"])
                )
                updated += 1

        if updated:
            conn.commit()
            log_fn(f"[COMPLEXITY] Auto-estimated complexity for {updated} backlog task(s)")
        conn.close()
    except Exception:
        pass  # best-effort


def _setup_logging_filters():
    """C-17/C-18: Install SecretFilter on root logger to redact secrets from all log output."""
    import logging as _logging
    try:
        from log_filter import SecretFilter
        root_logger = _logging.getLogger()
        if not any(isinstance(f, SecretFilter) for f in root_logger.filters):
            root_logger.addFilter(SecretFilter())
    except Exception:
        pass  # graceful degradation if log_filter not available


def _enforce_log_retention():
    """G-20: Delete log files older than configured retention period."""
    from pathlib import Path as _Path
    import json as _json
    import datetime as _dt
    settings_path = _Path.home() / '.automatica' / 'settings.json'
    try:
        cfg = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
        days = int(cfg.get('log_retention_days', 30))
        cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
        log_dir = _Path('logs')
        if log_dir.exists():
            for f in log_dir.glob('*.log'):
                try:
                    if _dt.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                        f.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def main():
    _enforce_log_retention()
    _setup_logging_filters()
    parser = argparse.ArgumentParser(
        description="Automatica autonomous sprint runner — runs sprints until project is done.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python run_autonomous.py --Project H:/Github/Pong3 --ProjectGoal "Make a cute 2D pong game"
  python run_autonomous.py --project-dir H:/Github/Pong3 --provider Ollama --max-minutes 30
""")
    # Friendly shorthand flags (--Project, --ProjectGoal, --Polling)
    parser.add_argument("--Project", dest="project_dir_short",
                        help="Project directory (shorthand for --project-dir)")
    parser.add_argument("--ProjectGoal", dest="project_goal",
                        help="Project goal — initializes project if not already set up")
    parser.add_argument("--Polling", dest="polling",
                        help="Polling interval, e.g. '5min' or '30s' (runs continuously at interval)")
    parser.add_argument("--Model", dest="model", default="llama3.2:3b",
                        help="Model name for local http_local_chat providers (default: llama3.2:3b)")
    # Management commands (single-shot operations, then exit)
    parser.add_argument("--AddTask", dest="add_task", metavar="TASK",
                        help="Add a task to the project backlog")
    parser.add_argument("--AddSprint", dest="add_sprint", metavar="GOAL",
                        help="Create a sprint with the given goal (dry-run planning)")
    parser.add_argument("--OrderBacklog", dest="order_backlog", action="store_true",
                        help="Reprioritise the backlog by priority and complexity")
    parser.add_argument("--SprintReview", dest="sprint_review", action="store_true",
                        help="Show a summary of the last sprint")
    parser.add_argument("--StartSprint", dest="start_sprint", action="store_true",
                        help="Run the next sprint (single sprint, then exit)")
    # Standard flags
    parser.add_argument("--project-dir", dest="project_dir")
    parser.add_argument("--provider", default=None,
                        help="AI provider name — must match a configured provider in the DB"
                             " (e.g. 'Ollama', 'OpenAI'). Required when running sprints.")
    parser.add_argument("--max-minutes", type=int, default=15)
    parser.add_argument("--max-sprints", type=int, default=10)
    parser.add_argument("--max-points", type=int, default=40)
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--no-replenish", dest="no_replenish", action="store_true",
                        help="Disable mid-run review-driven backlog replenishment "
                             "(default: refill backlog when empty so the loop "
                             "runs through the whole --max-minutes budget)")
    parser.add_argument("--observations-file", default=None)
    # Server mode — start REST+WebSocket API server for app/phone connections
    parser.add_argument("--Serve", dest="serve", action="store_true",
                        help="Start API server (REST+WS) for app/phone connections")
    parser.add_argument("--Port", dest="port", type=int, default=8765,
                        help="API server port (default: 8765)")
    parser.add_argument("--Host", dest="host", default="127.0.0.1",
                        help="API server bind host (default: 127.0.0.1 — use 0.0.0.0 for LAN access)")
    # Provider management (global — no --Project needed)
    parser.add_argument("--AddProvider", dest="add_provider_name", metavar="NAME",
                        help="Add an AI provider (use with --ProviderType, --ProviderUrl, --ProviderModel, --ProviderKeyEnv)")
    parser.add_argument("--ProviderType", dest="provider_type_new", metavar="TYPE",
                        help="Adapter type for --AddProvider (e.g. http_chat_completions, http_local_chat)")
    parser.add_argument("--ProviderUrl", dest="provider_url", metavar="URL",
                        help="Base URL for --AddProvider")
    parser.add_argument("--ProviderModel", dest="provider_model", metavar="MODEL",
                        help="Default model name for --AddProvider")
    parser.add_argument("--ProviderKeyEnv", dest="provider_key_env", metavar="VAR",
                        help="Env var name holding the API key for --AddProvider")
    parser.add_argument("--ListProviders", dest="list_providers", action="store_true",
                        help="List all configured AI providers")
    parser.add_argument("--RemoveProvider", dest="remove_provider", metavar="NAME_OR_ID",
                        help="Remove a provider by name or ID")
    # S5-W07: headless / smoke-test flag — parse-and-exit cleanly with no sprint execution
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Validate args and project dir, then exit without running sprints")
    # S5-W07: accept --project as alias for --Project / --project-dir
    parser.add_argument("--project", dest="project_dir_alias",
                        help="Alias for --Project / --project-dir")
    # S5-W07: --sprints as alias for --max-sprints
    parser.add_argument("--sprints", dest="max_sprints_alias", type=int, default=None,
                        help="Alias for --max-sprints")
    args = parser.parse_args()

    # --- Provider management commands (no --Project required) ---
    if args.list_providers:
        cmd_list_providers()
        return
    if args.add_provider_name:
        cmd_add_provider(
            name=args.add_provider_name,
            provider_type=args.provider_type_new or '',
            url=args.provider_url or '',
            model=args.provider_model or '',
            key_env=args.provider_key_env or '',
        )
        return
    if args.remove_provider:
        cmd_remove_provider(args.remove_provider)
        return

    # Resolve project dir (shorthand wins over --project-dir; --project alias also accepted)
    raw_project = args.project_dir_short or args.project_dir or getattr(args, 'project_dir_alias', None)
    # Apply --sprints alias
    if getattr(args, 'max_sprints_alias', None) is not None:
        args.max_sprints = args.max_sprints_alias

    # S5-W07: dry-run — validate and exit cleanly (used by headless smoke test)
    if getattr(args, 'dry_run', False):
        print(f"[DRY RUN] Project: {raw_project or '(none)'}")
        print("[DRY RUN] Args parsed OK. Exiting without running sprints.")
        return

    if not raw_project and not args.serve:
        parser.error("--Project / --project-dir is required (or use --Serve for server-only mode)")
    _pkg_dir = Path(__file__).resolve().parent

    # --- Server mode: start API server (optional, can combine with sprint loop) ---
    if args.serve:
        from api_server import APIServer as _APIServer
        _srv = _APIServer(host=args.host, port=args.port)
        _srv.start()
        display_host = 'localhost' if args.host in ('0.0.0.0', '') else args.host
        print(f"[SERVE] API server running on http://{display_host}:{args.port}")
        if not raw_project:
            # Server-only mode — no project, just serve until Ctrl+C
            print("[SERVE] No --Project specified. Server-only mode. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[SERVE] Stopped.")
            return

    project_dir = Path(raw_project).resolve()

    # Provider is required for any sprint/task execution
    _needs_provider = any([
        args.project_goal, args.add_sprint, args.order_backlog,
        args.start_sprint, not (args.sprint_review or args.add_task),
    ])
    if _needs_provider and not args.provider:
        parser.error(
            "--provider is required when running sprints or AI commands.\n"
            "  Configure a provider via: automatica --Serve  (then use the App)\n"
            "  Or specify one: --provider Ollama"
        )

    # Auto-initialize project if goal provided or project doesn't exist
    if args.project_goal or not (project_dir / ".automatica" / "project.db").exists():
        _init_project_if_needed(project_dir, args.project_goal or "", args.provider, args.model)

    # --- Single-shot management commands ---
    if args.add_task:
        cmd_add_task(project_dir, args.add_task)
        return
    if args.add_sprint:
        cmd_add_sprint(project_dir, args.add_sprint, _pkg_dir, args.provider)
        return
    if args.order_backlog:
        cmd_order_backlog(project_dir, _pkg_dir, args.provider)
        return
    if args.sprint_review:
        cmd_sprint_review(project_dir)
        return
    if args.start_sprint:
        args.max_sprints = 1  # run exactly one sprint, then exit

    obs_path = Path(args.observations_file) if args.observations_file else \
        (_pkg_dir / "logs" / f"autonomous_{project_dir.name}.md")
    obs_path.parent.mkdir(parents=True, exist_ok=True)

    def _safe(s: str) -> str:
        """Strip non-ASCII to avoid cp1252 UnicodeEncodeError on Windows console."""
        return s.encode("ascii", errors="replace").decode("ascii")

    deadline = time.time() + args.max_minutes * 60
    sprint_num = 1
    total_completed = 0
    consecutive_zero_velocity = 0  # tracks back-to-back sprints with 0 completed tasks
    obs_lines = [
        f"# Autonomous Run — {project_dir.name}",
        f"Started: {datetime.now().isoformat(timespec='seconds')}",
        f"Max time: {args.max_minutes} min | Max sprints: {args.max_sprints}",
        "",
    ]

    def write_obs(line: str):
        obs_lines.append(line)
        obs_path.write_text("\n".join(obs_lines), encoding="utf-8")
        try:
            print(_safe(line))
        except Exception:
            pass

    # Reset tasks stuck in mid-sprint states from previous crashed runs
    db_path = project_dir / ".automatica" / "project.db"
    if db_path.exists():
        import sqlite3 as _sq3
        _c = _sq3.connect(str(db_path))
        stuck = _c.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('in_progress','sprint_planned')"
        ).fetchone()[0]
        if stuck:
            _c.execute("UPDATE tasks SET status='backlog' WHERE status IN ('in_progress','sprint_planned')")
            _c.commit()
            print(f"[INIT] Reset {stuck} stuck task(s) to backlog")

        # Override max_points from project pipeline_config if set
        cfg_row = _c.execute(
            "SELECT value FROM project_settings WHERE key='pipeline_config'"
        ).fetchone()
        if cfg_row:
            try:
                cfg = json.loads(cfg_row[0])
                proj_max_pts = cfg.get("max_points_per_sprint")
                if proj_max_pts and isinstance(proj_max_pts, int):
                    # Only override if user didn't explicitly pass --max-points
                    if args.max_points == 40:  # default value
                        args.max_points = proj_max_pts
                        print(f"[INIT] max_points overridden from pipeline_config: {proj_max_pts}")
            except Exception:
                pass
        _c.close()

    write_obs(f"## Initial backlog")
    counts = load_backlog_counts(project_dir)
    write_obs(f"Tasks: {counts}")

    # ── Wake-up scan ────────────────────────────────────────────────────
    # Detect hand-edits made between Automatica runs and refresh the
    # corresponding summaries. The SummaryIndexer's hash-skip means only
    # files whose non-comment content actually diverged from the last
    # @automatica-summary tag get re-summarised — clean files are O(1)
    # AST parse + hash compare, no LLM call.
    try:
        _wake_up_scan(project_dir, args.provider, write_obs)
    except Exception as exc:
        write_obs(f"  [WAKEUP] scan failed: {exc}")

    # S3-W10: auto-estimate complexity for default-complexity backlog tasks
    _auto_estimate_complexity(project_dir)

    for sprint_num in range(1, args.max_sprints + 1):
        remaining_s = deadline - time.time()
        if remaining_s <= 0:
            write_obs(f"\n### STOPPED — time limit ({args.max_minutes} min) reached")
            break

        counts = load_backlog_counts(project_dir)
        available = counts["backlog"] + counts["sprint_planned"] + counts["in_progress"] + counts.get("blocked", 0)
        if available == 0:
            total_tasks = counts.get("total", 0) or 0
            # Fresh project (no tasks ever) on sprint #1 with a goal:
            # let the sprint runner do its first-pass goal->tasks generation.
            if sprint_num == 1 and counts.get("has_goal") and total_tasks == 0:
                write_obs(f"  Backlog empty — sprint runner will auto-generate tasks from project goal")
            elif counts.get("has_goal") and not getattr(args, "no_replenish", False):
                # Continuous-loop replenishment: instead of exiting on empty
                # mid-run (or running a no-op sprint when total>0), ask the
                # LLM to review the project and propose a fresh batch of
                # follow-on tasks (tests, refactors, perf, polish). Lets
                # `--max-minutes 720` actually mean 12 hours of progress
                # instead of "12 hours OR until the first 6 tasks finish,
                # whichever comes first".
                added = _replenish_via_review(project_dir, args.provider, write_obs)
                if added > 0:
                    write_obs(f"  Backlog replenished with {added} review-driven tasks; continuing")
                    counts = load_backlog_counts(project_dir)
                else:
                    write_obs(f"\n### DONE — backlog empty and review produced no new tasks "
                              f"after {sprint_num - 1} sprints")
                    break
            else:
                write_obs(f"\n### DONE — backlog empty after {sprint_num - 1} sprints")
                break

        write_obs(f"\n## Sprint #{sprint_num}")
        write_obs(f"Time remaining: {int(remaining_s/60)}m{int(remaining_s%60)}s")
        write_obs(f"Backlog status: {counts}")

        cmd = [
            sys.executable, "-u", str(_pkg_dir / "sprint_runner.py"),
            "--project-dir", str(project_dir),
            "--provider", args.provider,
            "--max-parallel", "1",
            "--max-points", str(args.max_points),
            "--max-iterations", str(args.max_iterations),
            # No --sprint-number: let sprint_runner auto-increment from DB
        ]

        sprint_start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                timeout=min(remaining_s - 10, args.max_minutes * 60),
                cwd=str(_pkg_dir),
            )
            duration = time.time() - sprint_start
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            # Extract JSON result dict from stdout (sprint_runner prints JSON at end)
            result = {}
            for line in reversed(stdout.splitlines()):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        result = parsed
                        break
                except Exception:
                    pass
            # Also try parsing the full stdout as a JSON object
            if not result:
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, dict):
                        result = parsed
                except Exception:
                    pass

            status = result.get("status", "unknown")
            completed = result.get("tasks_completed", 0)
            planned = result.get("tasks_planned", 0)
            points = result.get("points_completed", 0)
            total_completed += completed

            write_obs(f"Result: {status} | {completed}/{planned} tasks | {points}pts | {duration:.0f}s")

            # Sprint runner logs to stderr — show all (it's the main progress log)
            if stderr:
                write_obs(f"Sprint log:")
                for l in stderr.splitlines():
                    write_obs(f"  {l}")

            # Observations on failures
            if completed == 0 and planned > 0:
                write_obs(f"  [OBS] No tasks completed -- possible JSON parse issue or model loop")
                consecutive_zero_velocity += 1
                if consecutive_zero_velocity >= 3:
                    write_obs(
                        f"  [OBS] {consecutive_zero_velocity} consecutive zero-velocity sprints "
                        f"-- bumping complexity on blocked/failed tasks"
                    )
                    _bump_stalled_tasks(project_dir, write_obs)
                    consecutive_zero_velocity = 0
            else:
                consecutive_zero_velocity = 0

            if status == "failed" and planned == 0:
                write_obs(f"  [OBS] Sprint failed with 0 tasks planned -- check pipeline/DB state")
            if status == "empty":
                # Don't exit just because the planner picked 0 tasks. The
                # backlog can be 0 while blocked tasks sit in cooldown
                # (waiting for stale-recovery) — and in either case the
                # loop should try a review-driven replenish before giving
                # up. Empty sprints used to break the loop immediately,
                # which left a project at 7/8 done with one blocked task
                # and no path forward except a manual relaunch.
                if counts.get("has_goal") and not getattr(args, "no_replenish", False):
                    added = _replenish_via_review(project_dir, args.provider, write_obs)
                    if added > 0:
                        write_obs(f"  [OBS] Empty sprint — replenished with "
                                  f"{added} review-driven tasks; continuing")
                        continue
                write_obs(f"  [OBS] Backlog empty -- all done")
                break

        except subprocess.TimeoutExpired:
            write_obs(f"  [WARN] Sprint #{sprint_num} TIMED OUT after {args.max_minutes} min")
            break
        except Exception as exc:
            write_obs(f"  [ERR] Sprint #{sprint_num} ERROR: {exc}")
            break

    final_counts = load_backlog_counts(project_dir)
    write_obs(f"\n## Final State")
    write_obs(f"Ended: {datetime.now().isoformat(timespec='seconds')}")
    write_obs(f"Total completed across all sprints: {total_completed}")
    write_obs(f"Final task counts: {final_counts}")

if __name__ == "__main__":
    main()
