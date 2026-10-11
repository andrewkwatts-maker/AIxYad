#!/usr/bin/env python3
"""
sprint_summary.py — print a human-readable sprint table for an AIxYad project.

Usage:
    python scripts/sprint_summary.py --project PATH
    python scripts/sprint_summary.py --project . --limit 20
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aixyad_paths


def _fmt(value, width: int, align: str = "<") -> str:
    s = str(value) if value is not None else "-"
    if len(s) > width:
        s = s[:width - 1] + "…"
    return f"{s:{align}{width}}"


def print_sprint_table(project_dir: Path, limit: int = 20) -> int:
    db_path = aixyad_paths.project_state(project_dir) / "project.db"
    if not db_path.exists():
        print(f"ERROR: No project DB found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            sprint_number,
            status,
            tasks_completed,
            tasks_total,
            velocity,
            started_at,
            completed_at
        FROM sprints
        ORDER BY sprint_number DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    if not rows:
        print("No sprints found.")
        return 0

    # Header
    col_sprint  = 7
    col_status  = 18
    col_tasks   = 10
    col_vel     = 8
    col_date    = 19

    header = (
        _fmt("Sprint", col_sprint, ">") + "  " +
        _fmt("Status", col_status) + "  " +
        _fmt("Done/Total", col_tasks) + "  " +
        _fmt("Velocity", col_vel, ">") + "  " +
        _fmt("Started", col_date)
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    for r in rows:
        done_total = f"{r['tasks_completed'] or 0}/{r['tasks_total'] or 0}"
        vel = f"{r['velocity']:.1f}" if r["velocity"] is not None else "-"
        started = (r["started_at"] or "")[:19]
        print(
            _fmt(r["sprint_number"], col_sprint, ">") + "  " +
            _fmt(r["status"] or "-", col_status) + "  " +
            _fmt(done_total, col_tasks) + "  " +
            _fmt(vel, col_vel, ">") + "  " +
            _fmt(started, col_date)
        )

    print(sep)
    print(f"{len(rows)} sprint(s) shown (most recent first)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Print sprint table for an AIxYad project")
    parser.add_argument("--project", required=True, help="Path to project root directory")
    parser.add_argument("--limit", type=int, default=20, help="Max sprints to show (default: 20)")
    args = parser.parse_args()

    project_dir = Path(args.project).resolve()
    if not project_dir.exists():
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    sys.exit(print_sprint_table(project_dir, limit=args.limit))


if __name__ == "__main__":
    main()
