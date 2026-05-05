"""Integration: start API server, hit endpoints, check WS.

These tests require a running OS network stack and bind to real ports.
They are marked 'integration' so they are skipped in the default CI run.
Run them explicitly with: pytest -m integration
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.mark.integration
def test_server_status_endpoint():
    import threading
    import time
    import requests
    from api_server import APIServer
    srv = APIServer(port=18765)
    srv.start()
    time.sleep(1.0)
    try:
        r = requests.get('http://localhost:18765/api/status', timeout=5)
        assert r.status_code == 200
        assert r.json()['status'] == 'ok'
    finally:
        srv.stop()


@pytest.mark.integration
def test_server_projects_endpoint():
    import threading
    import time
    import requests
    from api_server import APIServer
    srv = APIServer(port=18766)
    srv.start()
    time.sleep(1.0)
    try:
        r = requests.get('http://localhost:18766/api/projects', timeout=5)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        srv.stop()
