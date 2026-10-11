#!/usr/bin/env python3
"""Tests for complexity-based provider routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aixyad_db as adb


def test_complexity_routing():
    """Test that providers are routed correctly by complexity."""
    # Create in-memory DB
    conn = adb.init_global_db(":memory:")

    # Use names that don't conflict with seeded defaults (Gemma3-4b etc.)
    p1 = adb.add_provider(conn, "TestSmall", "http_local_chat",
                          base_url="http://localhost:11434",
                          model_name="test:small")
    p2 = adb.add_provider(conn, "TestMedium", "http_local_chat",
                          base_url="http://localhost:11434",
                          model_name="test:medium")
    p3 = adb.add_provider(conn, "TestLarge", "http_local_chat",
                          base_url="http://localhost:11434",
                          model_name="test:large")

    # Set up routing manually using unique provider IDs
    adb.set_complexity_routing(conn, '*', 1, 3,  p1['id'], priority=10)
    adb.set_complexity_routing(conn, '*', 4, 8,  p2['id'], priority=10)
    adb.set_complexity_routing(conn, '*', 9, 89, p3['id'], priority=10)

    # Test routing for different complexity levels
    # Complexity 1-3 should route to TestSmall
    provider = adb.get_provider_for_complexity(conn, 1)
    assert provider is not None
    assert provider['name'] == 'TestSmall'

    provider = adb.get_provider_for_complexity(conn, 3)
    assert provider is not None
    assert provider['name'] == 'TestSmall'

    # Complexity 4-8 should route to TestMedium
    provider = adb.get_provider_for_complexity(conn, 5)
    assert provider is not None
    assert provider['name'] == 'TestMedium'

    provider = adb.get_provider_for_complexity(conn, 8)
    assert provider is not None
    assert provider['name'] == 'TestMedium'

    # Complexity >8 should route to TestLarge
    provider = adb.get_provider_for_complexity(conn, 13)
    assert provider is not None
    assert provider['name'] == 'TestLarge'

    provider = adb.get_provider_for_complexity(conn, 89)
    assert provider is not None
    assert provider['name'] == 'TestLarge'

    print("All complexity routing tests passed!")
    conn.close()


def test_custom_routing():
    """Test custom routing configuration using helper function."""
    conn = adb.init_global_db(":memory:")
    
    # Add providers
    adb.add_provider(conn, "Fast", "http_local_chat", 
                    model_name="gemma3:4b")
    adb.add_provider(conn, "Medium", "http_local_chat",
                    model_name="gemma3:12b")
    adb.add_provider(conn, "Powerful", "http_local_chat",
                    model_name="phi4")
    
    # Set up custom routing
    config = [
        {'min_complexity': 1, 'max_complexity': 2, 'provider_name': 'Fast'},
        {'min_complexity': 3, 'max_complexity': 8, 'provider_name': 'Medium'},
        {'min_complexity': 9, 'max_complexity': 89, 'provider_name': 'Powerful'},
    ]
    
    result = adb.setup_complexity_routing_by_name(conn, config)
    
    assert result['success']
    assert len(result['configured']) == 3
    assert len(result['skipped']) == 0
    
    # Verify routing
    assert adb.get_provider_for_complexity(conn, 1)['name'] == 'Fast'
    assert adb.get_provider_for_complexity(conn, 5)['name'] == 'Medium'
    assert adb.get_provider_for_complexity(conn, 13)['name'] == 'Powerful'
    
    print("✓ Custom routing tests passed!")
    conn.close()


def test_project_specific_routing():
    """Test project-specific routing overrides."""
    conn = adb.init_global_db(":memory:")
    
    # Add providers
    p1 = adb.add_provider(conn, "Default", "http_local_chat", model_name="gemma3:4b")
    p2 = adb.add_provider(conn, "ProjectSpecific", "http_local_chat", model_name="phi4")
    
    # Set global default
    adb.set_complexity_routing(conn, '*', 1, 89, p1['id'], priority=0)
    
    # Set project-specific override
    adb.set_complexity_routing(conn, 'myproject', 1, 89, p2['id'], priority=0)
    
    # Global should use Default
    provider = adb.get_provider_for_complexity(conn, 5, project_name='*')
    assert provider['name'] == 'Default'
    
    # myproject should use ProjectSpecific
    provider = adb.get_provider_for_complexity(conn, 5, project_name='myproject')
    assert provider['name'] == 'ProjectSpecific'
    
    # other projects should fall back to global default
    provider = adb.get_provider_for_complexity(conn, 5, project_name='otherproject')
    assert provider['name'] == 'Default'
    
    print("✓ Project-specific routing tests passed!")
    conn.close()


if __name__ == '__main__':
    test_complexity_routing()
    test_custom_routing()
    test_project_specific_routing()
    print("\n✅ All tests passed!")
