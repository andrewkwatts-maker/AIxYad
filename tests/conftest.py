"""
conftest.py — pytest configuration for AIxYad tests.

Adds the App directory to sys.path so that services/api_client.py
can be imported from tests in the AIxYad (engine) repo.
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
# <App>/services/secrets.py — starlette.responses needs stdlib secrets.
# Try the new sibling name first; fall back to the old one while that folder
# still hasn't been renamed (repo folder renames are the ecosystem's last step).
_APP_SIBLING_DIR = Path(__file__).resolve().parent.parent.parent
_APP_ROOT_NEW = _APP_SIBLING_DIR / 'AIxYad-App'
_APP_ROOT_OLD = _APP_SIBLING_DIR / 'Automatica-App'
_APP_ROOT = str(_APP_ROOT_NEW if _APP_ROOT_NEW.exists() else _APP_ROOT_OLD)
_APP_SERVICES = str(Path(_APP_ROOT) / 'services')
for _p in (_APP_ROOT, _APP_SERVICES):
    if _p not in sys.path:
        sys.path.append(_p)
