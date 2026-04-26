"""
conftest.py — pytest configuration for Automatica tests.

Adds the Automatica-App directory to sys.path so that services/api_client.py
can be imported from tests in the Automatica (engine) repo.
"""
import sys
from pathlib import Path

# Add the engine repo root (H:\Github\Automatica) to sys.path — must be first
# so `from api_server import APIServer` finds the engine, not the app re-exporter.
_ENGINE_ROOT = str(Path(__file__).resolve().parent.parent)
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

# Add the App repo so services/api_client.py is importable.
# Appended (not inserted at 0) so the stdlib `secrets` module is found before
# Automatica-App/services/secrets.py — starlette.responses needs stdlib secrets.
_APP_ROOT = str(Path(__file__).resolve().parent.parent.parent / 'Automatica-App')
_APP_SERVICES = str(Path(_APP_ROOT) / 'services')
for _p in (_APP_ROOT, _APP_SERVICES):
    if _p not in sys.path:
        sys.path.append(_p)
