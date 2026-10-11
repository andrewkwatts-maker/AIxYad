#!/usr/bin/env python3
"""
One-shot setup script for Nova3D + AIxYad integration.

  1. Verify/update Claude-Sonnet provider config in global DB
  2. Register Nova3D in known_projects (global DB)
  3. Init project DB via AIxYad APIs, import all TASKS.md tasks
  4. Write AGENTS.md for agent context
  5. Print launch instructions

Run from H:/Github/Automatica:
    python setup_nova3d.py
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import aixyad_paths

AIXYAD_DIR  = Path(__file__).parent
GLOBAL_DB_PATH  = aixyad_paths.global_db_path()
NOVA_DIR        = Path(r"H:\Github\Nova3D")
NOVA_TASKS_FILE = NOVA_DIR / "TASKS.md"
PROVIDER_NAME   = "Claude-Sonnet"

CLAUDE_CONFIG = {
    "executable": "claude",
    # `--permission-mode acceptEdits` pre-authorises file edits (which the
    # agent loop needs to do its job) while still gating shell commands,
    # web fetches, and other dangerous tools behind Claude Code's normal
    # permission prompts. This replaces the previous
    # `--dangerously-skip-permissions` flag, which was a blanket security
    # void. State-changing API endpoints (POST/PATCH/DELETE /api/providers)
    # are still gated by the Bearer / X-API-Key / session-token check in
    # api_server.py, so the credential path is preserved.
    "args": ["--permission-mode", "acceptEdits", "-p"],
    "timeout": 600,
    "mode": "agentic",
}

PRIORITY_MAP = {"P0": "CRITICAL", "P1": "HIGH", "P2": "MEDIUM", "P3": "LOW"}

# Lamport tier -> Fibonacci complexity estimate
def lamport_to_complexity(lamport: int) -> int:
    if lamport <= 2:   return 1    # tooling fix
    if lamport <= 13:  return 5    # single stub
    if lamport <= 24:  return 3    # test writing
    if lamport <= 56:  return 8    # medium optimisation
    if lamport <= 105: return 13   # architecture design doc
    if lamport <= 124: return 21   # MMO backend systems
    if lamport <= 135: return 13   # geo systems
    if lamport <= 149: return 21   # WC3 gameplay systems
    return 34                      # UE5 graphics tier

def aixyad_status(tasks_status: str) -> str:
    return {"done": "done", "skipped": "archived"}.get(tasks_status, "backlog")


# ---------------------------------------------------------------------------
# 1. Provider verification / update
# ---------------------------------------------------------------------------

def configure_provider(gconn: sqlite3.Connection) -> None:
    print(f"\n[1] Verifying '{PROVIDER_NAME}' provider in global DB")
    cur = gconn.execute(
        "SELECT id, config_json FROM providers WHERE name = ?", (PROVIDER_NAME,)
    )
    row = cur.fetchone()
    if row is None:
        print("    NOT FOUND — inserting new subprocess_cli provider")
        gconn.execute(
            """INSERT INTO providers (name, provider_type, enabled, config_json)
               VALUES (?, 'subprocess_cli', 1, ?)""",
            (PROVIDER_NAME, json.dumps(CLAUDE_CONFIG)),
        )
    else:
        current = json.loads(row[1]) if row[1] else {}
        if current != CLAUDE_CONFIG:
            gconn.execute(
                "UPDATE providers SET config_json = ? WHERE name = ?",
                (json.dumps(CLAUDE_CONFIG), PROVIDER_NAME),
            )
            print(f"    UPDATED config_json (was: {row[1]})")
        else:
            print("    OK: config_json already correct")
    gconn.commit()


# ---------------------------------------------------------------------------
# 2. Register Nova3D in known_projects
# ---------------------------------------------------------------------------

NOVA_GOAL = (
    "Build a full geo-located MMO RTS C++ game engine: "
    "WC3-level gameplay (Hero/Ability/Fog-of-War/Resources), "
    "UE5-level graphics (Lumen GI via RadianceCascade, VSM, TAA, Nanite LOD), "
    "authoritative game server (20 Hz tick, client prediction, rollback), "
    "persistent world state (buildings survive offline), "
    "geolocation (GPS->geohash territories, location-based building placement). "
    "Fix all engine stubs; implement production-quality C++17."
)

def register_project(gconn: sqlite3.Connection) -> None:
    print(f"\n[2] Registering Nova3D in known_projects")
    row = gconn.execute(
        "SELECT id, name FROM known_projects WHERE directory = ?", (str(NOVA_DIR),)
    ).fetchone()
    if row:
        gconn.execute(
            "UPDATE known_projects SET name = ?, goal = ?, last_opened = ? WHERE id = ?",
            ("Nova3D", NOVA_GOAL, datetime.now(timezone.utc).isoformat(), row[0]),
        )
        print(f"    UPDATED existing project id={row[0]}")
    else:
        gconn.execute(
            """INSERT INTO known_projects (name, directory, goal, created_at)
               VALUES (?, ?, ?, ?)""",
            ("Nova3D", str(NOVA_DIR), NOVA_GOAL,
             datetime.now(timezone.utc).isoformat()),
        )
        print("    INSERTED new project")
    gconn.commit()


# ---------------------------------------------------------------------------
# 3. Parse TASKS.md
# ---------------------------------------------------------------------------

TASK_BLOCK_RE = re.compile(r"---TASK_START---\s*(.*?)\s*---TASK_END---", re.DOTALL)
FIELD_RE = {
    "lamport":  re.compile(r"\[LAMPORT:\s*(\d+)\]"),
    "priority": re.compile(r"\[PRIORITY:\s*(\S+)\]"),
    "agent":    re.compile(r"\[AGENT:\s*(\S+)\]"),
    "status":   re.compile(r"\[STATUS:\s*(\S+)\]"),
    "depends":  re.compile(r"\[DEPENDS:\s*(.+?)\]"),
}
ID_TITLE_RE = re.compile(r"^(\S+)\s+-\s+(.+)$", re.MULTILINE)


def _split_body(body: str) -> tuple[str, str]:
    """Split task body into (description, acceptance_criteria)."""
    desc_lines, ac_lines, in_ac = [], [], False
    for line in body.splitlines():
        s = line.strip()
        if s.lower().startswith("acceptance criteria"):
            in_ac = True
            continue
        if in_ac:
            ac_lines.append(s)
        elif not s.startswith("["):
            desc_lines.append(s)
    description = "\n".join(l for l in desc_lines if l).strip()
    acceptance  = "\n".join(ac_lines).strip()
    return description, acceptance


def parse_tasks(path: Path) -> list[dict]:
    text   = path.read_text(encoding="utf-8")
    blocks = TASK_BLOCK_RE.findall(text)
    tasks  = []

    for body in blocks:
        t: dict = {
            "lamport": 0, "priority": "P2", "agent": "Developer",
            "status": "todo", "depends": [], "id": "", "title": "", "body": body.strip(),
        }
        for field, rx in FIELD_RE.items():
            m = rx.search(body)
            if m:
                t[field] = m.group(1).strip()

        t["lamport"] = int(t["lamport"]) if t["lamport"] else 0

        dep_raw = t["depends"]
        if isinstance(dep_raw, str) and dep_raw.lower() not in ("none", ""):
            # Strip "L:" prefix so we get raw integers
            t["depends"] = [
                re.sub(r"^L:", "", d.strip()) for d in dep_raw.split(",") if d.strip()
            ]
        else:
            t["depends"] = []

        for line in body.splitlines():
            line = line.strip()
            if line.startswith("[") or line.startswith("#") or not line:
                continue
            m = ID_TITLE_RE.match(line)
            if m:
                t["id"], t["title"] = m.group(1), m.group(2)
                break

        if t["lamport"] > 0 and t["title"]:
            tasks.append(t)

    return sorted(tasks, key=lambda x: x["lamport"])


# ---------------------------------------------------------------------------
# 4. Import tasks into AIxYad project DB
# ---------------------------------------------------------------------------

def _add_column(conn: sqlite3.Connection, table: str, col: str, defn: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def import_tasks(tasks: list[dict]) -> None:
    print(f"\n[3] Importing {len(tasks)} tasks into AIxYad project DB")

    sys.path.insert(0, str(AIXYAD_DIR))
    from aixyad_db import init_project_db  # noqa: PLC0415

    conn = init_project_db(NOVA_DIR)

    # Extend schema with Nova3D-specific columns (idempotent)
    _add_column(conn, "tasks", "lamport",        "INTEGER DEFAULT 0")
    _add_column(conn, "tasks", "depends_lamport", "TEXT    DEFAULT ''")

    # Build a lamport -> task DB id map from existing rows
    lamport_to_id: dict[int, int] = {
        row[0]: row[1]
        for row in conn.execute("SELECT lamport, id FROM tasks WHERE lamport > 0").fetchall()
    }

    inserted = skipped = 0

    for t in tasks:
        display_id = f"L{t['lamport']:04d}-{t['id']}"

        # Skip if already present
        existing = conn.execute(
            "SELECT id FROM tasks WHERE display_id = ?", (display_id,)
        ).fetchone()
        if existing:
            lamport_to_id[t["lamport"]] = existing[0]
            skipped += 1
            continue

        priority   = PRIORITY_MAP.get(t["priority"], "MEDIUM")
        complexity = lamport_to_complexity(t["lamport"])
        status     = aixyad_status(t["status"])
        agent_slug = t["agent"].lower()
        description, acceptance = _split_body(t["body"])

        # Wire first declared dependency to AIxYad's FK column
        depends_on_task_id: int | None = None
        if t["depends"]:
            first_dep = t["depends"][0]
            dep_lamport = int(first_dep) if first_dep.isdigit() else None
            if dep_lamport is not None and dep_lamport in lamport_to_id:
                depends_on_task_id = lamport_to_id[dep_lamport]

        depends_lamport_str = ",".join(t["depends"])

        cur = conn.execute(
            """INSERT INTO tasks (
                   display_id, title, description, acceptance_criteria,
                   status, priority, complexity, agent_slug,
                   sort_order, depends_on_task_id,
                   lamport, depends_lamport
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                display_id, t["title"], description, acceptance,
                status, priority, complexity, agent_slug,
                t["lamport"],          # sort_order = lamport for natural ordering
                depends_on_task_id,
                t["lamport"], depends_lamport_str,
            ),
        )
        task_id = cur.lastrowid
        lamport_to_id[t["lamport"]] = task_id
        inserted += 1

    conn.commit()
    conn.close()
    print(f"    Inserted: {inserted}  |  Skipped (already exist): {skipped}")


# ---------------------------------------------------------------------------
# 5. Write AGENTS.md
# ---------------------------------------------------------------------------

AGENTS_MD = """\
# Nova3D Engine -- Agent Context

## Project Goal
Build a full geo-located MMO RTS C++ game engine with:
- WC3-level gameplay (Hero units, abilities, fog of war, resource system)
- UE5-level graphics (Lumen GI via RadianceCascade, Virtual Shadow Maps, TAA, Nanite-style LOD)
- Authoritative game server (20Hz tick, client prediction, rollback)
- Persistent world state (buildings/processes survive server restart + offline simulation)
- Geolocation (GPS -> game world, geohash territories, location-based building placement)

## Build System
- CMake 3.20+, MSVC on Windows, target: Release
- Build: cmake --build build --config Release -j
- Test:  ctest --test-dir build -C Release -j4 --output-on-failure

## Coding Standards (CRITICAL)
- Language: C++17
- ALL JSON includes must use engine/core/json_wrapper.hpp -- NEVER nlohmann/json.hpp directly
- No raw new/delete -- always unique_ptr / shared_ptr
- No magic numbers -- name every constant
- No silent failures -- use spdlog::error + return Result/bool
- No placeholder comments -- finish the function or remove it
- All implementations must compile. All existing tests must pass.
- Write at least 1 unit test per new public function (GoogleTest)

## SOLID Principles (enforced)
- SRP: one reason to change per class
- OCP: new behaviour via new types, not if/else chains
- LSP: derived classes fully substitutable
- ISP: narrow interfaces only
- DIP: depend on abstractions, not concretions

## Key Directory Map
- engine/game/        -- RTS game logic (BuildingUI, WallPlacement, etc.)
- engine/graphics/    -- Renderer, SDF, RadianceCascade, LOD, post-process
- engine/terrain/     -- SDFTerrain, VoxelTerrain
- engine/networking/  -- FirebaseClient, game server networking
- engine/editor/      -- Editor panels, PlayMode, PCGPanel
- engine/import/      -- TextureImporter, ModelImporter, AnimationImporter
- engine/persistence/ -- EntitySerializer, SQLite save system
- engine/server/      -- Authoritative game server (to be built)
- engine/location/    -- GPS/geolocation systems
- game/src/rts/       -- RTS units, buildings, tech tree
- game/src/entities/  -- Hero, Ability, Item systems
- game/src/systems/   -- FogOfWar, ResourceSystem, VictorySystem
- tests/              -- GoogleTest unit and integration tests

## Agent Assignments
- developer:    C++ stub implementation, feature code, game systems
- tester:       GoogleTest test files, coverage
- architect:    Design docs in docs/architecture/, system contracts
- documentarian: Doxygen comments on public APIs
"""


def write_agents_md() -> None:
    target = NOVA_DIR / "AGENTS.md"
    print(f"\n[4] Writing {target}")
    target.write_text(AGENTS_MD, encoding="utf-8")
    print("    OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  AIxYad x Nova3D Setup")
    print("=" * 60)

    if not NOVA_TASKS_FILE.exists():
        sys.exit(f"ERROR: {NOVA_TASKS_FILE} not found")

    sys.path.insert(0, str(AIXYAD_DIR))
    from aixyad_db import init_global_db  # noqa: PLC0415

    gconn = init_global_db(GLOBAL_DB_PATH)
    configure_provider(gconn)
    register_project(gconn)
    gconn.close()

    tasks = parse_tasks(NOVA_TASKS_FILE)
    print(f"\n    Parsed {len(tasks)} tasks from TASKS.md")
    import_tasks(tasks)

    write_agents_md()

    print("\n" + "=" * 60)
    print("  Setup complete.")
    print()
    print("  Start autonomous server (runs sprints indefinitely):")
    print()
    print("    cd H:\\Github\\Automatica")
    print("    python run_autonomous.py \\")
    print("      --Serve --Port 8765 \\")
    print("      --Project H:\\Github\\Nova3D \\")
    print(f'      --provider "{PROVIDER_NAME}" \\')
    print("      --max-sprints 0 --max-minutes 0")
    print()
    print("  Trigger a single sprint (from another terminal):")
    print("    curl -X POST http://localhost:8765/api/sprints/start")
    print()
    print("  Monitor tasks:")
    print("    curl http://localhost:8765/api/projects/nova3d/tasks | python -m json.tool")
    print("=" * 60)


if __name__ == "__main__":
    main()
