"""
Tests for aixyad_paths — the Automatica -> AIxYad migration helper.

Covers: one-time directory/file migration from the old ``.automatica`` /
``automatica.db`` names, that migration never overwrites an existing new
location, and that env() prefers AIXYAD_<name> but falls back to the old
AUTOMATICA_<name>.
"""
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aixyad_paths


# ── home() ──────────────────────────────────────────────────────────────────

def test_home_migrates_old_folder_once(tmp_path, monkeypatch):
    """~/.automatica is renamed to ~/.aixyad the first time home() is called."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    old = tmp_path / ".automatica"
    old.mkdir()
    (old / "settings.json").write_text("{}", encoding="utf-8")

    new = aixyad_paths.home()

    assert new == tmp_path / ".aixyad"
    assert new.exists()
    assert (new / "settings.json").exists()
    assert not old.exists()


def test_home_creates_new_folder_when_neither_exists(tmp_path, monkeypatch):
    """With no old folder present, home() just returns the new path (not created)."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    new = aixyad_paths.home()

    assert new == tmp_path / ".aixyad"
    assert not (tmp_path / ".automatica").exists()


def test_home_never_overwrites_existing_new_folder(tmp_path, monkeypatch):
    """If ~/.aixyad already exists, an old ~/.automatica is left untouched —
    migration never clobbers data that's already in the new location."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    new = tmp_path / ".aixyad"
    new.mkdir()
    (new / "settings.json").write_text('{"already": "migrated"}', encoding="utf-8")

    old = tmp_path / ".automatica"
    old.mkdir()
    (old / "settings.json").write_text('{"stale": "data"}', encoding="utf-8")

    result = aixyad_paths.home()

    assert result == new
    # Old data is never deleted, even though it wasn't used.
    assert old.exists()
    assert (old / "settings.json").read_text(encoding="utf-8") == '{"stale": "data"}'
    # New data is untouched.
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"already": "migrated"}'


# ── project_state() ─────────────────────────────────────────────────────────

def test_project_state_migrates_old_folder_once(tmp_path):
    """<project>/.automatica is renamed to <project>/.aixyad once."""
    old = tmp_path / ".automatica"
    old.mkdir()
    (old / "project.db").write_bytes(b"sqlite-data")

    new = aixyad_paths.project_state(tmp_path)

    assert new == tmp_path / ".aixyad"
    assert (new / "project.db").read_bytes() == b"sqlite-data"
    assert not old.exists()


def test_project_state_never_overwrites_existing_new_folder(tmp_path):
    """An existing <project>/.aixyad is left alone; old data is never deleted."""
    new = tmp_path / ".aixyad"
    new.mkdir()
    (new / "project.db").write_bytes(b"new-data")

    old = tmp_path / ".automatica"
    old.mkdir()
    (old / "project.db").write_bytes(b"old-data")

    result = aixyad_paths.project_state(tmp_path)

    assert result == new
    assert (new / "project.db").read_bytes() == b"new-data"
    assert old.exists()
    assert (old / "project.db").read_bytes() == b"old-data"


def test_project_state_idempotent_when_already_migrated(tmp_path):
    """Calling project_state() again after migration is a harmless no-op."""
    new = aixyad_paths.project_state(tmp_path)
    new.mkdir()
    (new / "marker.txt").write_text("x", encoding="utf-8")

    again = aixyad_paths.project_state(tmp_path)

    assert again == new
    assert (again / "marker.txt").read_text(encoding="utf-8") == "x"


# ── global_db_path() ────────────────────────────────────────────────────────

def test_global_db_path_migrates_db_file_once(tmp_path):
    """automatica.db is renamed to aixyad.db the first time it's resolved."""
    old_db = tmp_path / "automatica.db"
    old_db.write_bytes(b"sqlite-bytes")

    new_db = aixyad_paths.global_db_path(base_dir=tmp_path)

    assert new_db == tmp_path / "aixyad.db"
    assert new_db.read_bytes() == b"sqlite-bytes"
    assert not old_db.exists()


def test_global_db_path_never_overwrites_existing_new_db(tmp_path):
    """An existing aixyad.db is left alone even if automatica.db also exists."""
    new_db = tmp_path / "aixyad.db"
    new_db.write_bytes(b"new-db")
    old_db = tmp_path / "automatica.db"
    old_db.write_bytes(b"old-db")

    result = aixyad_paths.global_db_path(base_dir=tmp_path)

    assert result == new_db
    assert new_db.read_bytes() == b"new-db"
    assert old_db.exists()
    assert old_db.read_bytes() == b"old-db"


def test_global_db_path_defaults_to_home(tmp_path, monkeypatch):
    """With no base_dir, the global DB lives under home()."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = aixyad_paths.global_db_path()

    assert result == tmp_path / ".aixyad" / "aixyad.db"


# ── env() ───────────────────────────────────────────────────────────────────

def test_env_prefers_new_name(monkeypatch):
    monkeypatch.setenv("AIXYAD_API_KEY", "new-key")
    monkeypatch.setenv("AUTOMATICA_API_KEY", "old-key")

    assert aixyad_paths.env("API_KEY") == "new-key"


def test_env_falls_back_to_old_name(monkeypatch):
    monkeypatch.delenv("AIXYAD_API_KEY", raising=False)
    monkeypatch.setenv("AUTOMATICA_API_KEY", "old-key")

    assert aixyad_paths.env("API_KEY") == "old-key"


def test_env_returns_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("AIXYAD_API_KEY", raising=False)
    monkeypatch.delenv("AUTOMATICA_API_KEY", raising=False)

    assert aixyad_paths.env("API_KEY", "fallback-default") == "fallback-default"


def test_env_returns_none_default_when_unset(monkeypatch):
    monkeypatch.delenv("AIXYAD_LOCAL_AI", raising=False)
    monkeypatch.delenv("AUTOMATICA_LOCAL_AI", raising=False)

    assert aixyad_paths.env("LOCAL_AI") is None
