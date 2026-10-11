"""
Integration test for project_scanner.build_file_wiki()
"""
import json
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_scanner import build_file_wiki


def test_build_file_wiki_indexes_all_files(tmp_path):
    """Test that build_file_wiki() finds all files with correct metadata."""
    # Create 5 test files with GUID markers
    test_files = [
        ("file1.py", "# GUID:abc-123\ndef hello():\n    pass\n"),
        ("file2.cpp", "// GUID:def-456\nvoid world() {}\n"),
        ("subdir/file3.js", "// GUID:ghi-789\nfunction test() {}\n"),
        ("subdir/file4.py", "# GUID:jkl-012\n# GUID:mno-345\nclass Test:\n    pass\n"),
        ("file5.md", "# README\n\nSome text\n"),
    ]
    
    # Create directory structure and files
    (tmp_path / "subdir").mkdir(parents=True)
    for rel_path, content in test_files:
        (tmp_path / rel_path).write_text(content, encoding="utf-8")
    
    # Call build_file_wiki
    result = build_file_wiki(tmp_path)
    
    # Assert structure
    assert "files" in result
    assert "generated_at" in result
    assert "total_files" in result
    assert "total_lines" in result
    
    # Assert all files indexed
    assert result["total_files"] == 5
    indexed_paths = {f["path"] for f in result["files"]}
    expected_paths = {"file1.py", "file2.cpp", "subdir/file3.js", "subdir/file4.py", "file5.md"}
    assert indexed_paths == expected_paths
    
    # Assert GUIDs found
    file1 = next(f for f in result["files"] if f["path"] == "file1.py")
    assert "abc-123" in file1["guids"]
    
    file2 = next(f for f in result["files"] if f["path"] == "file2.cpp")
    assert "def-456" in file2["guids"]
    
    file3 = next(f for f in result["files"] if f["path"] == "subdir/file3.js")
    assert "ghi-789" in file3["guids"]
    
    file4 = next(f for f in result["files"] if f["path"] == "subdir/file4.py")
    assert "jkl-012" in file4["guids"]
    assert "mno-345" in file4["guids"]
    
    file5 = next(f for f in result["files"] if f["path"] == "file5.md")
    assert file5["guids"] == []


def test_build_file_wiki_caches_to_json(tmp_path):
    """Test that build_file_wiki() writes cache to .aixyad/wiki_index.json."""
    # Create a test file
    (tmp_path / "test.py").write_text("# GUID:test-001\npass\n", encoding="utf-8")

    # Call build_file_wiki (should create cache)
    result = build_file_wiki(tmp_path)

    # Assert cache file exists
    cache_path = tmp_path / ".aixyad" / "wiki_index.json"
    assert cache_path.exists()
    
    # Assert cache content matches result
    cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached_data["total_files"] == result["total_files"]
    assert cached_data["total_lines"] == result["total_lines"]
    assert len(cached_data["files"]) == len(result["files"])


def test_build_file_wiki_uses_cache_when_fresh(tmp_path):
    """Test that build_file_wiki() uses cache when no files are newer."""
    # Create a test file
    test_file = tmp_path / "test.py"
    test_file.write_text("# GUID:cached-001\npass\n", encoding="utf-8")
    
    # First call creates cache
    result1 = build_file_wiki(tmp_path)
    timestamp1 = result1["generated_at"]
    
    # Second call should use cache (same timestamp)
    result2 = build_file_wiki(tmp_path)
    timestamp2 = result2["generated_at"]
    
    assert timestamp1 == timestamp2
    assert result1["total_files"] == result2["total_files"]


def test_build_file_wiki_refreshes_cache_on_file_change(tmp_path):
    """Test that build_file_wiki() refreshes cache when files are newer."""
    import time
    
    # Create initial file
    test_file = tmp_path / "test.py"
    test_file.write_text("# GUID:old-001\npass\n", encoding="utf-8")
    
    # First call creates cache
    result1 = build_file_wiki(tmp_path)
    assert result1["total_files"] == 1
    
    # Wait a bit and modify file (to ensure different mtime)
    time.sleep(0.1)
    test_file.write_text("# GUID:new-002\ndef updated():\n    pass\n", encoding="utf-8")
    
    # Second call should detect change and rebuild
    result2 = build_file_wiki(tmp_path)
    assert result2["total_files"] == 1
    
    # Verify GUID was updated
    file_data = result2["files"][0]
    assert "new-002" in file_data["guids"]
    assert "old-001" not in file_data["guids"]


def test_build_file_wiki_file_metadata(tmp_path):
    """Test that build_file_wiki() includes correct file metadata."""
    # Create test file with known content
    content = "# GUID:meta-test\n" + "x = 1\n" * 10
    test_file = tmp_path / "meta.py"
    test_file.write_text(content, encoding="utf-8")
    
    # Call build_file_wiki
    result = build_file_wiki(tmp_path)
    
    # Get file metadata
    file_data = result["files"][0]
    
    # Assert metadata fields exist
    assert "path" in file_data
    assert "size_bytes" in file_data
    assert "line_count" in file_data
    assert "language" in file_data
    assert "last_modified" in file_data
    assert "mtime_epoch" in file_data
    assert "guids" in file_data
    
    # Assert values are reasonable
    assert file_data["path"] == "meta.py"
    assert file_data["size_bytes"] > 0
    assert file_data["line_count"] == 11  # header + 10 lines
    assert file_data["language"] == "python"
    assert len(file_data["last_modified"]) > 0
    assert file_data["mtime_epoch"] > 0


def test_build_file_wiki_total_lines(tmp_path):
    """Test that build_file_wiki() correctly counts total lines across all files."""
    # Create files with known line counts
    (tmp_path / "file1.py").write_text("# 5 lines\na\nb\nc\nd\n", encoding="utf-8")
    (tmp_path / "file2.py").write_text("# 3 lines\nx\ny\n", encoding="utf-8")
    
    result = build_file_wiki(tmp_path)
    
    # Total should be 5 + 3 = 8
    assert result["total_lines"] == 8
