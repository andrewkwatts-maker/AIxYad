#!/usr/bin/env python3
"""
backup_db.py — back up .automatica/project.db to a timestamped copy.

Usage:
    python scripts/backup_db.py                        # backs up current dir's project
    python scripts/backup_db.py --project PATH         # backs up a specific project
    python scripts/backup_db.py --project PATH --keep 5
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import sys
from pathlib import Path


MAX_BACKUPS = 10


def backup_db(project_dir: Path, keep: int = MAX_BACKUPS) -> int:
    db_path = project_dir / ".automatica" / "project.db"
    if not db_path.exists():
        print(f"ERROR: No project DB found at {db_path}", file=sys.stderr)
        return 1

    backup_dir = project_dir / ".automatica" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"project_{ts}.db"

    shutil.copy2(str(db_path), str(dest))
    size_kb = dest.stat().st_size / 1024
    print(f"Saved: {dest}  ({size_kb:.1f} KB)")

    # Prune old backups — keep newest N
    existing = sorted(backup_dir.glob("project_*.db"))
    to_delete = existing[:-keep] if len(existing) > keep else []
    for old in to_delete:
        old.unlink()
        print(f"Pruned: {old.name}")

    remaining = len(list(backup_dir.glob("project_*.db")))
    print(f"{remaining} backup(s) retained in {backup_dir}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Back up an Automatica project DB")
    parser.add_argument("--project", default=".", help="Project root directory (default: .)")
    parser.add_argument("--keep", type=int, default=MAX_BACKUPS,
                        help=f"Number of backups to keep (default: {MAX_BACKUPS})")
    args = parser.parse_args()

    project_dir = Path(args.project).resolve()
    if not project_dir.exists():
        print(f"ERROR: Directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    sys.exit(backup_db(project_dir, keep=args.keep))


if __name__ == "__main__":
    main()
