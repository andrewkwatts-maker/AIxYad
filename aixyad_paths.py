#!/usr/bin/env python3
"""aixyad_paths — single source of truth for AIxYad's on-disk locations.

AIxYad was renamed from Automatica. This module decides where the
user-global and per-project state live under the new name, and migrates
data from the old ``.automatica`` / ``automatica.db`` names the first time
it's asked for the new path. Migration never deletes old data: it is a
single rename (move), performed at most once, and skipped entirely if the
new location already exists (so a partially-migrated setup is never
clobbered).

Env vars follow the same pattern: ``AIXYAD_<NAME>`` is read first, falling
back to the old ``AUTOMATICA_<NAME>`` so existing setups keep working
without changes.
"""

import os
from pathlib import Path

_OLD_ENV_PREFIX = "AUTOMATICA_"
_NEW_ENV_PREFIX = "AIXYAD_"

_OLD_HOME_NAME = ".automatica"
_NEW_HOME_NAME = ".aixyad"

_OLD_DB_NAME = "automatica.db"
_NEW_DB_NAME = "aixyad.db"


def _migrate(old: Path, new: Path) -> None:
    """Move old -> new once, if new doesn't exist and old does. Never deletes."""
    try:
        if not new.exists() and old.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
    except OSError:
        pass  # leave both in place rather than risk losing data


def home() -> Path:
    """The user-global AIxYad state folder: ``~/.aixyad``.

    Migrates ``~/.automatica`` -> ``~/.aixyad`` once, the first time this is
    called, if the new folder doesn't exist yet and the old one does.
    """
    base = Path.home()
    new = base / _NEW_HOME_NAME
    old = base / _OLD_HOME_NAME
    _migrate(old, new)
    return new


def project_state(project_root) -> Path:
    """A project's AIxYad state folder: ``<project_root>/.aixyad``.

    Migrates ``<project_root>/.automatica`` -> ``<project_root>/.aixyad``
    once, the first time this is called for that project.
    """
    root = Path(project_root)
    new = root / _NEW_HOME_NAME
    old = root / _OLD_HOME_NAME
    _migrate(old, new)
    return new


def global_db_path(base_dir=None) -> Path:
    """Where the global AIxYad database file lives.

    If ``base_dir`` is given, the file is ``<base_dir>/aixyad.db`` (e.g. next
    to the frozen EXE or the repo root). Otherwise it is
    ``home()/aixyad.db`` (the ``~/.aixyad`` user state folder). Migrates the
    old ``automatica.db`` name once, in either location.
    """
    base = home() if base_dir is None else Path(base_dir)
    old = base / _OLD_DB_NAME
    new = base / _NEW_DB_NAME
    _migrate(old, new)
    return new


def env(name: str, default=None):
    """Read ``AIXYAD_<name>`` first, falling back to the old ``AUTOMATICA_<name>``.

    Returns ``default`` if neither is set.
    """
    new_key = _NEW_ENV_PREFIX + name
    old_key = _OLD_ENV_PREFIX + name
    if new_key in os.environ:
        return os.environ[new_key]
    if old_key in os.environ:
        return os.environ[old_key]
    return default
