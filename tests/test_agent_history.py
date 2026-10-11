#!/usr/bin/env python3
"""Test agent history compression and storage."""

import sqlite3
import tempfile
from pathlib import Path

from aixyad_db import (
    init_project_db,
    create_task,
    create_sprint,
    store_history_entry,
    store_history_batch,
    load_recent_history,
    load_iteration_history,
    get_history_stats,
    clear_history,
    compress_content,
    decompress_content,
    estimate_tokens,
)


def test_compression():
    """Test compression and decompression."""
    original = "This is a test message with some content."
    compressed = compress_content(original)
    decompressed = decompress_content(compressed)
    
    assert decompressed == original
    assert len(compressed) < len(original)  # Should be compressed
    print(f"[OK] Compression: {len(original)} -> {len(compressed)} bytes")


def test_token_estimation():
    """Test token estimation."""
    text = "Hello world, this is a test."
    tokens = estimate_tokens(text)
    
    # Rough estimate: ~4 chars per token
    expected = len(text) // 4
    assert tokens == expected
    print(f"[OK] Token estimation: {len(text)} chars -> {tokens} tokens")


def test_history_storage():
    """Test storing and loading history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = init_project_db(Path(tmpdir))
        
        # Create a task
        task = create_task(conn, "Test task", complexity=5)
        task_id = task["id"]
        
        # Create a sprint
        sprint = create_sprint(conn, 1, "Test sprint", "Testing")
        sprint_id = sprint["id"]
        
        # Store single entry
        content1 = "User: Please implement feature X"
        entry_id = store_history_entry(conn, task_id, sprint_id, 1, "user", content1)
        assert entry_id > 0
        print(f"[OK] Stored single entry: id={entry_id}")
        
        # Store batch
        messages = [
            {"role": "assistant", "content": "I will implement feature X"},
            {"role": "system", "content": "Action completed successfully"},
        ]
        ids = store_history_batch(conn, task_id, sprint_id, 1, messages)
        assert len(ids) == 2
        print(f"[OK] Stored batch: {len(ids)} entries")
        
        # Load recent history
        history = load_recent_history(conn, task_id, limit=10)
        assert len(history) == 3
        assert history[0]["content"] == content1
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        print(f"[OK] Loaded recent history: {len(history)} entries")
        
        # Load iteration history
        iter_history = load_iteration_history(conn, task_id, 1)
        assert len(iter_history) == 3
        print(f"[OK] Loaded iteration 1 history: {len(iter_history)} entries")
        
        # Add more entries for iteration 2
        store_history_batch(conn, task_id, sprint_id, 2, [
            {"role": "user", "content": "Now add feature Y"},
            {"role": "assistant", "content": "Working on feature Y"},
        ])
        
        # Get stats
        stats = get_history_stats(conn, task_id)
        assert stats["entry_count"] == 5
        assert stats["max_iteration"] == 2
        print(f"[OK] History stats: {stats}")
        
        # Test clearing old history
        deleted = clear_history(conn, task_id, before_iteration=2)
        assert deleted == 3
        print(f"[OK] Cleared {deleted} entries before iteration 2")
        
        # Verify only iteration 2 remains
        remaining = load_recent_history(conn, task_id, limit=10)
        assert len(remaining) == 2
        assert all(h["iteration"] == 2 for h in remaining)
        print(f"[OK] Remaining entries: {len(remaining)}")
        
        conn.close()


def test_compression_ratio():
    """Test compression ratio on realistic data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = init_project_db(Path(tmpdir))
        
        task = create_task(conn, "Test task", complexity=5)
        task_id = task["id"]
        
        # Simulate realistic conversation
        messages = [
            {"role": "user", "content": "Please analyze the file src/main.py and identify potential bugs."},
            {"role": "assistant", "content": "I've analyzed src/main.py. Here are the issues I found:\n1. Line 45: Potential null pointer\n2. Line 67: Missing error handling\n3. Line 89: Race condition in threading code\n\nWould you like me to fix these issues?"},
            {"role": "user", "content": "Yes, please fix all three issues."},
            {"role": "assistant", "content": "I'll fix all three issues. Starting with the null pointer check on line 45..."},
        ]
        
        # Calculate uncompressed size
        uncompressed_size = sum(len(m["content"]) for m in messages)
        
        # Store with compression
        store_history_batch(conn, task_id, None, 1, messages)
        
        # Get compressed size from DB
        stats = get_history_stats(conn, task_id)
        compressed_size = stats["compressed_bytes"]
        
        ratio = uncompressed_size / compressed_size if compressed_size > 0 else 0
        print(f"[OK] Compression ratio: {uncompressed_size} -> {compressed_size} bytes ({ratio:.2f}x)")
        
        # Verify we can decompress correctly
        loaded = load_recent_history(conn, task_id, limit=10)
        assert len(loaded) == len(messages)
        assert loaded[0]["content"] == messages[0]["content"]
        print(f"[OK] Decompression verified")
        
        conn.close()


if __name__ == "__main__":
    print("=== Testing Agent History Storage ===\n")
    
    test_compression()
    test_token_estimation()
    test_history_storage()
    test_compression_ratio()
    
    print("\n=== All tests passed ===")
