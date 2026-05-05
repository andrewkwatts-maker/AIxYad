"""One-shot migration: replace `--dangerously-skip-permissions` in existing
provider rows with the safer `--permission-mode acceptEdits` flag.

This script is idempotent and never deletes rows. Run from the Automatica dir:

    python scripts/migrate_provider_security.py [--dry-run] [--db PATH]

By default it operates on the global Automatica DB at
~/.automatica/automatica.db (and falls back to ./automatica.db).

Why: previously the Claude provider config carried
`--dangerously-skip-permissions` which is a blanket security void. The agent
loop only needs to make file edits, so `--permission-mode acceptEdits` is
sufficient — it auto-approves edits while still gating shell commands and
other dangerous tools through Claude Code's normal permission prompts.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_DANGEROUS_FLAG = "--dangerously-skip-permissions"
_REPLACEMENT_FLAGS = ["--permission-mode", "acceptEdits"]


def _default_db_path() -> Path:
    home = Path.home() / ".automatica" / "automatica.db"
    if home.exists():
        return home
    here = Path(__file__).resolve().parent.parent / "automatica.db"
    return here


def _patch_args(args: list) -> tuple[list, bool]:
    """Return (new_args, changed). Replace the dangerous flag with the safer
    permission-mode flags exactly once; leave already-safe configs alone."""
    if _DANGEROUS_FLAG not in args:
        return args, False
    new_args: list = []
    replaced = False
    for tok in args:
        if tok == _DANGEROUS_FLAG:
            if not replaced:
                new_args.extend(_REPLACEMENT_FLAGS)
                replaced = True
            # if already replaced once, drop subsequent occurrences
        else:
            new_args.append(tok)
    return new_args, True


def migrate(db_path: Path, dry_run: bool = False) -> int:
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, config_json FROM providers WHERE config_json IS NOT NULL"
    ).fetchall()

    changed = 0
    for row in rows:
        try:
            cfg = json.loads(row["config_json"]) if row["config_json"] else {}
        except json.JSONDecodeError:
            print(f"  SKIP id={row['id']} {row['name']!r}: invalid config_json")
            continue
        if not isinstance(cfg, dict):
            continue
        old_args = cfg.get("args")
        if not isinstance(old_args, list):
            continue
        new_args, did = _patch_args(old_args)
        if not did:
            continue
        cfg["args"] = new_args
        cfg.setdefault("mode", "agentic")  # ensure agentic mode on safer flag
        new_json = json.dumps(cfg)
        action = "WOULD UPDATE" if dry_run else "UPDATED"
        print(f"  {action} id={row['id']} {row['name']!r}:")
        print(f"      old args: {old_args}")
        print(f"      new args: {new_args}")
        if not dry_run:
            conn.execute(
                "UPDATE providers SET config_json = ?, "
                "       updated_at = datetime('now') WHERE id = ?",
                (new_json, row["id"]),
            )
        changed += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\nSummary: {changed} provider row(s) {'would be ' if dry_run else ''}updated.")
    if changed == 0:
        print("All provider configs already use safe permission flags.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=None,
                   help=f"DB path (default: {_default_db_path()})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would change but make no DB writes.")
    args = p.parse_args()
    db = args.db or _default_db_path()
    print(f"Migrating providers in {db}{' (dry-run)' if args.dry_run else ''}\n")
    return migrate(db, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
