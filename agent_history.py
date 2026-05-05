#!/usr/bin/env python3
"""
Agent History Storage — Compressed conversation history persistence.

Stores agent conversation history with zlib compression to save space.
Supports loading last N items on task resume.
"""

import zlib
import json
import sqlite3
from typing import List, Dict, Optional, Any
from datetime import datetime


def estimate_tokens(text: str) -> int:
    """
    Rough token estimate: ~4 chars per token for English text.
    This is a simple heuristic; real tokenization would be more accurate.
    """
    return len(text) // 4


def compress_content(content: str) -> bytes:
    """Compress content using zlib."""
    return zlib.compress(content.encode('utf-8'))


def decompress_content(compressed: bytes) -> str:
    """Decompress zlib-compressed content."""
    return zlib.decompress(compressed).decode('utf-8')


def store_history_entry(
    conn: sqlite3.Connection,
    task_id: int,
    sprint_id: Optional[int],
    iteration: int,
    role: str,
    content: str
) -> int:
    """
    Store a single history entry with compression.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        sprint_id: Optional sprint ID
        iteration: Iteration number
        role: Message role (user/assistant/system)
        content: Message content
        
    Returns:
        ID of inserted history entry
    """
    content_compressed = compress_content(content)
    token_estimate = estimate_tokens(content)
    
    cur = conn.execute("""
        INSERT INTO agent_history 
        (task_id, sprint_id, iteration, role, content_compressed, token_estimate)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (task_id, sprint_id, iteration, role, content_compressed, token_estimate))
    
    conn.commit()
    return cur.lastrowid


def store_history_batch(
    conn: sqlite3.Connection,
    task_id: int,
    sprint_id: Optional[int],
    iteration: int,
    messages: List[Dict[str, str]]
) -> List[int]:
    """
    Store multiple history entries in a batch.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        sprint_id: Optional sprint ID
        iteration: Iteration number
        messages: List of message dicts with 'role' and 'content' keys
        
    Returns:
        List of IDs of inserted history entries
    """
    ids = []
    for msg in messages:
        role = msg.get('role', 'assistant')
        content = msg.get('content', '')
        
        content_compressed = compress_content(content)
        token_estimate = estimate_tokens(content)
        
        cur = conn.execute("""
            INSERT INTO agent_history 
            (task_id, sprint_id, iteration, role, content_compressed, token_estimate)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, sprint_id, iteration, role, content_compressed, token_estimate))
        
        ids.append(cur.lastrowid)
    
    conn.commit()
    return ids


def load_recent_history(
    conn: sqlite3.Connection,
    task_id: int,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Load the most recent N history entries for a task.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        limit: Maximum number of entries to load (default 20)
        
    Returns:
        List of history entries with decompressed content
    """
    cur = conn.execute("""
        SELECT id, task_id, sprint_id, iteration, role, 
               content_compressed, token_estimate, created_at
        FROM agent_history
        WHERE task_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (task_id, limit))
    
    rows = cur.fetchall()
    
    # Reverse so oldest comes first
    history = []
    for row in reversed(rows):
        content = decompress_content(row[5])
        history.append({
            'id': row[0],
            'task_id': row[1],
            'sprint_id': row[2],
            'iteration': row[3],
            'role': row[4],
            'content': content,
            'token_estimate': row[6],
            'created_at': row[7]
        })
    
    return history


def load_iteration_history(
    conn: sqlite3.Connection,
    task_id: int,
    iteration: int
) -> List[Dict[str, Any]]:
    """
    Load all history entries for a specific iteration.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        iteration: Iteration number
        
    Returns:
        List of history entries with decompressed content
    """
    cur = conn.execute("""
        SELECT id, task_id, sprint_id, iteration, role, 
               content_compressed, token_estimate, created_at
        FROM agent_history
        WHERE task_id = ? AND iteration = ?
        ORDER BY id ASC
    """, (task_id, iteration))
    
    rows = cur.fetchall()
    
    history = []
    for row in rows:
        content = decompress_content(row[5])
        history.append({
            'id': row[0],
            'task_id': row[1],
            'sprint_id': row[2],
            'iteration': row[3],
            'role': row[4],
            'content': content,
            'token_estimate': row[6],
            'created_at': row[7]
        })
    
    return history


def get_history_stats(
    conn: sqlite3.Connection,
    task_id: int
) -> Dict[str, Any]:
    """
    Get statistics about stored history for a task.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        
    Returns:
        Dict with count, total_tokens, and size info
    """
    cur = conn.execute("""
        SELECT 
            COUNT(*) as entry_count,
            SUM(token_estimate) as total_tokens,
            SUM(LENGTH(content_compressed)) as compressed_bytes,
            MAX(iteration) as max_iteration
        FROM agent_history
        WHERE task_id = ?
    """, (task_id,))
    
    row = cur.fetchone()
    
    return {
        'entry_count': row[0] or 0,
        'total_tokens': row[1] or 0,
        'compressed_bytes': row[2] or 0,
        'max_iteration': row[3] or 0
    }


def clear_history(
    conn: sqlite3.Connection,
    task_id: int,
    before_iteration: Optional[int] = None
) -> int:
    """
    Clear history entries for a task.
    
    Args:
        conn: Database connection
        task_id: ID of the task
        before_iteration: If specified, only clear entries before this iteration
        
    Returns:
        Number of entries deleted
    """
    if before_iteration is not None:
        cur = conn.execute("""
            DELETE FROM agent_history
            WHERE task_id = ? AND iteration < ?
        """, (task_id, before_iteration))
    else:
        cur = conn.execute("""
            DELETE FROM agent_history
            WHERE task_id = ?
        """, (task_id,))
    
    conn.commit()
    return cur.rowcount
