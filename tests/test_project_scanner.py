"""
pytest unit tests for project_scanner.list_project_files()
"""
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_scanner import list_project_files


def test_returns_list_of_dicts(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "c.py").write_text("z = 3\n")

    result = list_project_files(tmp_path)

    assert isinstance(result, list)
    assert len(result) == 3


def test_dicts_have_required_keys(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")

    result = list_project_files(tmp_path)

    assert len(result) == 1
    item = result[0]
    assert "path" in item
    assert "language" in item


def test_correct_file_count(tmp_path):
    for i in range(5):
        (tmp_path / f"file_{i}.py").write_text(f"# {i}\n")

    result = list_project_files(tmp_path)

    assert len(result) == 5


def test_extension_filter(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "notes.txt").write_text("hello\n")

    result = list_project_files(tmp_path, extension=".py")

    assert len(result) == 2
    for item in result:
        assert item["path"].endswith(".py")


def test_paths_are_relative_strings(tmp_path):
    (tmp_path / "module.py").write_text("pass\n")

    result = list_project_files(tmp_path)

    assert len(result) == 1
    assert isinstance(result[0]["path"], str)
    # Should be relative, not absolute
    assert not Path(result[0]["path"]).is_absolute()


def test_language_detected_for_python(tmp_path):
    (tmp_path / "script.py").write_text("import os\n")

    result = list_project_files(tmp_path)

    assert result[0]["language"].lower() in ("python", "py")


def test_empty_directory(tmp_path):
    result = list_project_files(tmp_path)
    assert result == []
