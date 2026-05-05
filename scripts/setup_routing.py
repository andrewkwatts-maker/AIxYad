#!/usr/bin/env python3
"""
Setup complexity-based provider routing for Automatica.

Usage:
    python scripts/setup_routing.py
    python scripts/setup_routing.py --project myproject
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from automatica_db import init_global_db, setup_complexity_routing_by_name


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Setup complexity routing')
    parser.add_argument('--project', default='*', 
                       help='Project name (default: * for global)')
    args = parser.parse_args()
    
    # Default routing configuration
    routing_config = [
        # Fast local model for simple tasks
        {
            'min_complexity': 1,
            'max_complexity': 3,
            'provider_name': 'Gemma3-4b',
            'priority': 0
        },
        # Better local model for moderate tasks
        {
            'min_complexity': 4,
            'max_complexity': 8,
            'provider_name': 'Gemma3-12b',
            'priority': 0
        },
        # Powerful local model for complex tasks
        {
            'min_complexity': 9,
            'max_complexity': 89,
            'provider_name': 'Phi4',
            'priority': 0
        },
        # Claude as fallback for complex tasks (if configured)
        {
            'min_complexity': 9,
            'max_complexity': 89,
            'provider_name': 'Claude',
            'priority': 1
        },
    ]
    
    print("Initializing global database...")
    conn = init_global_db()
    
    print(f"\nSetting up complexity routing for project: {args.project}")
    result = setup_complexity_routing_by_name(conn, routing_config, args.project)
    
    if result['configured']:
        print("\n✓ Configured routing rules:")
        for rule in result['configured']:
            print(f"  • {rule}")
    
    if result['skipped']:
        print("\n⚠ Skipped (providers not found or disabled):")
        for skip in result['skipped']:
            print(f"  • {skip}")
    
    print(f"\nTotal: {len(result['configured'])} rules configured")
    
    # Show current routing
    print("\nCurrent routing configuration:")
    rows = conn.execute("""
        SELECT cr.*, p.name as provider_name
        FROM complexity_routing cr
        JOIN providers p ON cr.provider_id = p.id
        WHERE cr.project_name = ? OR cr.project_name = '*'
        ORDER BY cr.min_complexity, cr.priority
    """, (args.project,)).fetchall()
    
    for row in rows:
        print(f"  {row['min_complexity']}-{row['max_complexity']}: "
              f"{row['provider_name']} (priority {row['priority']}, "
              f"project: {row['project_name']}, "
              f"enabled: {bool(row['enabled'])})")
    
    conn.close()
    print("\n✓ Done!")


if __name__ == '__main__':
    main()
