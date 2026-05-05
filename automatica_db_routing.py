#!/usr/bin/env python3
"""
Complexity routing setup for Automatica.

This module provides a function to seed default complexity-based provider routing:
- complexity <= 3 → gemma3:4b (fast)
- complexity 4-8 → gemma3:12b (better)
- complexity > 8 → phi4 or claude (if configured)
"""

import sqlite3
from typing import Optional


def setup_complexity_routing(conn: sqlite3.Connection, project_name: str = '*') -> dict:
    """Set up complexity-based provider routing.
    
    Routes tasks to different providers based on complexity:
    - complexity <= 3 → gemma3:4b (fast, local)
    - complexity 4-8 → gemma3:12b (better quality, still local)
    - complexity > 8 → phi4 or claude (if available)
    
    Args:
        conn: SQLite connection to global DB
        project_name: Project name ('*' for global default)
        
    Returns:
        dict with 'success' and details about what was configured
    """
    # Get all providers with their IDs and names
    providers = {}
    for row in conn.execute("SELECT id, name, model_name, enabled FROM providers").fetchall():
        providers[row['name']] = {
            'id': row['id'],
            'model': row['model_name'],
            'enabled': row['enabled']
        }
    
    configured = []
    skipped = []
    
    # Route 1: complexity <= 3 → gemma3:4b (fast)
    gemma4b = None
    for name in ['Gemma3-4b', 'gemma3:4b', 'fast']:
        if name in providers and providers[name]['enabled']:
            gemma4b = providers[name]['id']
            break
    
    if gemma4b:
        conn.execute("""
            INSERT OR REPLACE INTO complexity_routing 
            (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
            VALUES (?, 1, 3, ?, 0, 1)
        """, (project_name, gemma4b))
        configured.append("complexity 1-3 → gemma3:4b")
    else:
        skipped.append("gemma3:4b not found for complexity 1-3")
    
    # Route 2: complexity 4-8 → gemma3:12b (better)
    gemma12b = None
    for name in ['Gemma3-12b', 'gemma3:12b', 'medium']:
        if name in providers and providers[name]['enabled']:
            gemma12b = providers[name]['id']
            break
    
    if gemma12b:
        conn.execute("""
            INSERT OR REPLACE INTO complexity_routing 
            (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
            VALUES (?, 4, 8, ?, 0, 1)
        """, (project_name, gemma12b))
        configured.append("complexity 4-8 → gemma3:12b")
    else:
        skipped.append("gemma3:12b not found for complexity 4-8")
    
    # Route 3: complexity > 8 → phi4 (priority 0) or claude (priority 1)
    phi4 = None
    for name in ['Phi4', 'phi4', 'powerful']:
        if name in providers and providers[name]['enabled']:
            phi4 = providers[name]['id']
            break
    
    if phi4:
        conn.execute("""
            INSERT OR REPLACE INTO complexity_routing 
            (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
            VALUES (?, 9, 89, ?, 0, 1)
        """, (project_name, phi4))
        configured.append("complexity 9-89 → phi4 (priority 0)")
    else:
        skipped.append("phi4 not found for complexity > 8")
    
    # Try to add Claude as fallback for complexity > 8
    claude = None
    for name in ['Claude', 'claude', 'claude-sonnet']:
        if name in providers and providers[name]['enabled']:
            claude = providers[name]['id']
            break
    
    if claude:
        conn.execute("""
            INSERT OR REPLACE INTO complexity_routing 
            (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
            VALUES (?, 9, 89, ?, 1, 1)
        """, (project_name, claude))
        configured.append("complexity 9-89 → claude (priority 1, fallback)")
    else:
        skipped.append("claude not available as fallback for complexity > 8")
    
    conn.commit()
    
    return {
        'success': True,
        'configured': configured,
        'skipped': skipped,
        'total_rules': len(configured)
    }


if __name__ == '__main__':
    # Test/demo
    import sys
    from pathlib import Path
    
    # Import from parent directory
    sys.path.insert(0, str(Path(__file__).parent))
    from automatica_db import init_global_db
    
    print("Setting up complexity routing...")
    conn = init_global_db()
    result = setup_complexity_routing(conn)
    
    print(f"\nSuccess: {result['success']}")
    print(f"Configured {result['total_rules']} routing rules:")
    for rule in result['configured']:
        print(f"  ✓ {rule}")
    
    if result['skipped']:
        print("\nSkipped (providers not found):")
        for skip in result['skipped']:
            print(f"  - {skip}")
    
    conn.close()
