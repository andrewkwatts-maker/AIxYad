#!/usr/bin/env python3
"""
Automatica API Server — FastAPI + Uvicorn REST + WebSocket server.

Runs in a daemon thread so it can be embedded inside `automatica --Serve`
or started from the KivyMD app (server mode).

Usage (headless):
    automatica --Serve [--Host 0.0.0.0] [--Port 8765]

Usage (from code):
    from api_server import APIServer
    srv = APIServer(host='0.0.0.0', port=8765)
    srv.start()

Endpoints (Sprint E — full implementation):
    GET  /api/status              Health check + server info
    GET  /api/projects            List known projects
    GET  /api/projects/{id}/tasks Task list for a project
    POST /api/projects/{id}/tasks Add a task
    POST /api/sprints/start       Trigger sprint runner
    GET  /api/logs                Paginated action_log
    WS   /ws/logs                 Real-time log stream (JWT auth)
    WS   /ws/status               Live sprint/task status updates
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import threading
import time as _time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ── B-17 / C-06: Pydantic request models (module-level so FastAPI/OpenAPI resolves them) ──

try:
    from pydantic import BaseModel, Field, validator
    from typing import Optional as _ModelOpt

    class TaskCreate(BaseModel):
        title: str = Field(..., min_length=1, max_length=500)
        description: str = Field('', max_length=10000)
        complexity: int = Field(3, ge=1, le=13)

    class TaskUpdate(BaseModel):
        title: _ModelOpt[str] = Field(None, max_length=500)
        description: _ModelOpt[str] = Field(None, max_length=10000)
        status: _ModelOpt[str] = None
        complexity: _ModelOpt[int] = Field(None, ge=1, le=13)

    class SprintCreate(BaseModel):
        project_id: int
        goal: str = Field('', max_length=2000)
        max_points: int = Field(100, ge=1, le=1000)

    class AgentCreate(BaseModel):
        # C-06/C-08: strict slug pattern
        slug: str = Field(..., pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')
        role: str = Field('', max_length=200)
        description: str = Field('', max_length=10000)
        prompt: str = Field('', max_length=10000)

    class StandardSave(BaseModel):
        name: str = Field(..., min_length=1, max_length=200)
        content: str = Field('', max_length=100000)

    _PYDANTIC_AVAILABLE = True
except ImportError:
    _PYDANTIC_AVAILABLE = False

# ── C-04: In-memory token bucket rate limiter ───────────────────────────────
_rate_buckets: dict[str, list] = defaultdict(list)
_RATE_LIMIT  = 60    # requests per minute per IP
_RATE_WINDOW = 60.0  # seconds

# ── C-16: Session token store ───────────────────────────────────────────────
import secrets as _secrets
import hashlib as _hashlib

_sessions: dict[str, float] = {}  # token → expiry timestamp (monotonic)
SESSION_TTL = 3600  # 1 hour

# ── H2-02: Pending pairing tokens ───────────────────────────────────────────
_pending_tokens: dict = {}  # token -> {created: timestamp}

def _get_global_conn():
    """Get or create the global DB connection for auth endpoints."""
    from pathlib import Path
    import automatica_db as adb
    db_path = Path.home() / '.automatica' / 'automatica.db'
    return adb.init_global_db(db_path)


def _safe_load_settings() -> dict:
    """Read ~/.automatica/settings.json; return {} on any error.
    Used by the startup-gate + auth-middleware before _build_app spins up."""
    from pathlib import Path
    settings_path = Path.home() / '.automatica' / 'settings.json'
    if not settings_path.exists():
        return {}
    try:
        return _json.loads(settings_path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _push_event_sync(event_type: str, data: dict) -> None:
    """Module-level helper: push a WS event if the API server is running.

    Safe to call from any thread; silently no-ops when the server is not up.
    """
    try:
        # Import here to avoid circular imports at module load time
        import importlib
        _mod = importlib.import_module(__name__)
        # Walk all live APIServer thread locals to find a running app
        for _thread in threading.enumerate():
            if getattr(_thread, 'name', '').startswith('automatica-api-server'):
                # The server is running; the app state is held inside the
                # uvicorn server which doesn't expose it directly, so we rely
                # on the _push_event_sync_target set by the server at startup.
                break
        fn = _PUSH_EVENT_FN
        if fn:
            fn(event_type, data)
    except Exception:
        pass


# Set by APIServer._run() after the app is built so _push_event_sync can reach it.
_PUSH_EVENT_FN = None


class APIServer:
    """FastAPI + Uvicorn server running in a daemon thread."""

    def __init__(self, host: str = '127.0.0.1', port: int = 8765):
        self.host = host
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._server = None

    def start(self):
        """Start the server in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='automatica-api-server')
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.should_exit = True

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def url(self) -> str:
        host = 'localhost' if self.host in ('0.0.0.0', '') else self.host
        return f'http://{host}:{self.port}'

    def _run(self):
        try:
            import uvicorn
            from fastapi import FastAPI

            # ── HARDENING: refuse to bind to non-loopback without an API key ──
            # 0.0.0.0 / external IP exposed to LAN/WAN must require auth.
            # Localhost-only is allowed in dev mode without auth (other local
            # processes can still hit it but the attack surface is only this PC).
            _host_is_loopback = self.host in ('127.0.0.1', 'localhost', '::1')
            _has_key = bool(
                os.environ.get('AUTOMATICA_API_KEY', '')
                or _safe_load_settings().get('api_key', '')
            )
            _force_open = os.environ.get('AUTOMATICA_ALLOW_UNAUTH', '') == '1'
            if not _host_is_loopback and not _has_key and not _force_open:
                raise RuntimeError(
                    f"REFUSING to bind {self.host}:{self.port} without an API key. "
                    f"Either: set AUTOMATICA_API_KEY env var (preferred), or write "
                    f"'api_key' into ~/.automatica/settings.json, or override the "
                    f"safety with AUTOMATICA_ALLOW_UNAUTH=1 (NOT recommended). "
                    f"Loopback-only deployments (127.0.0.1) skip this check."
                )

            app = self._build_app()

            # C-12: optional SSL support from settings.json
            settings_path = Path.home() / '.automatica' / 'settings.json'
            cfg: dict = {}
            if settings_path.exists():
                try:
                    cfg = _json.loads(settings_path.read_text())
                except Exception:
                    pass
            ssl_certfile = cfg.get('ssl_certfile') or None
            ssl_keyfile  = cfg.get('ssl_keyfile') or None

            config = uvicorn.Config(
                app, host=self.host, port=self.port,
                log_level='warning',
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
            self._server = uvicorn.Server(config)

            # C-18: install SecretFilter on uvicorn loggers
            try:
                from log_filter import SecretFilter as _SF
                import logging as _logging
                for _lname in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
                    _lger = _logging.getLogger(_lname)
                    if not any(isinstance(f, _SF) for f in _lger.filters):
                        _lger.addFilter(_SF())
            except Exception:
                pass

            # Capture event loop so push_event can schedule coroutines thread-safely
            # asyncio.get_event_loop() raises in threads on Python 3.10+; create a new one
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            app.state.loop = _loop
            # Expose push_event globally so _push_event_sync can call it
            global _PUSH_EVENT_FN
            _PUSH_EVENT_FN = app.state.push_event
            self._server.run()
        except ImportError:
            # fastapi/uvicorn not installed — warn clearly
            print('[API SERVER] fastapi/uvicorn not installed. '
                  'Run: pip install automatica[server]')
        except Exception as exc:
            print(f'[API SERVER] Error: {exc}')

    def _build_app(self):
        from fastapi import (FastAPI, HTTPException, WebSocket, WebSocketDisconnect,
                             Depends, Security, Request)
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
        from pathlib import Path as _Path
        from typing import Optional as _Opt
        import time as _t

        # Module-level Pydantic models are used directly (B-17).
        # Local aliases so endpoints can reference them inside the closure.
        _AgentCreate   = AgentCreate    # noqa: F821 (defined at module level)
        _TaskUpdate    = TaskUpdate     # noqa: F821
        _SprintCreate  = SprintCreate   # noqa: F821
        _StandardSave  = StandardSave   # noqa: F821

        # ── B-18 / F-18: FastAPI app with full metadata ──────────────────────
        app = FastAPI(
            title='Automatica API',
            version='1.0.0',
            description=(
                'REST + WebSocket API for Automatica autonomous agent system.\n\n'
                'See /docs for interactive documentation, /redoc for ReDoc, '
                'or /openapi.json for the raw OpenAPI spec.'
            ),
            docs_url='/docs',
            redoc_url='/redoc',
            contact={
                'name': 'Automatica Project',
                'url': 'https://github.com/andrewkwatts-maker/Automatica',
            },
            license_info={
                'name': 'MIT',
                'url': 'https://opensource.org/licenses/MIT',
            },
        )

        # ── C-05: Restricted CORS ─────────────────────────────────────────────
        def _get_allowed_origins() -> list:
            settings_path = _Path.home() / '.automatica' / 'settings.json'
            try:
                cfg = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
                origins = cfg.get('allowed_origins', ['http://localhost', 'http://127.0.0.1'])
                return origins if isinstance(origins, list) else ['http://localhost']
            except Exception:
                return ['http://localhost', 'http://127.0.0.1']

        # CORS — never use '*' here. Default to localhost-only origins.
        _origins = _get_allowed_origins()
        if '*' in _origins:
            import logging as _log_cors
            _log_cors.getLogger(__name__).warning(
                "CORS allow_origins contains '*' — refusing. Falling back to localhost-only."
            )
            _origins = ['http://localhost', 'http://127.0.0.1']
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'],
            allow_headers=['Authorization', 'Content-Type', 'X-API-Key'],
            allow_credentials=True,
        )

        # ── HARDENING: default-deny auth middleware ────────────────────────────
        # Whitelist a small set of public routes. Everything else requires a
        # valid Bearer token / session token / X-API-Key when an API key is
        # configured. When NO key is configured, only loopback callers get
        # through (catches the "forgot to set API key but bound to LAN" case).
        _PUBLIC_ROUTES = frozenset({
            '/api/health',           # health probe
            '/api/auth/login',       # need to be reachable to issue a token
            '/api/auth/qr',          # device-pairing handshake start
            '/api/auth/pair',        # device-pairing handshake finish
            '/api/openapi.json', '/openapi.json',
            '/docs', '/redoc',
        })
        _LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}

        @app.middleware('http')
        async def _auth_gate(request: Request, call_next):
            path = request.url.path or ''
            # Always allow OPTIONS preflight + the public whitelist
            if request.method == 'OPTIONS' or path in _PUBLIC_ROUTES:
                return await call_next(request)
            # Pull credentials from Authorization header OR X-API-Key OR ?token=
            token = ''
            auth_h = request.headers.get('authorization') or ''
            if auth_h.lower().startswith('bearer '):
                token = auth_h[7:].strip()
            if not token:
                token = (request.headers.get('x-api-key') or
                         request.query_params.get('token') or '')
            expected_key = (
                os.environ.get('AUTOMATICA_API_KEY', '')
                or _safe_load_settings().get('api_key', '')
            )
            if not expected_key:
                # No key configured → only loopback callers allowed.
                client = (request.client.host if request.client else '')
                if client not in _LOOPBACK_HOSTS:
                    return JSONResponse(
                        {'error': 'Authentication required (server has no API key '
                                  'configured and you are not on loopback). Set '
                                  'AUTOMATICA_API_KEY env var or settings.json '
                                  'api_key field.'},
                        status_code=401,
                    )
                return await call_next(request)
            # Key configured: token must match it OR be a live session token.
            if token == expected_key:
                return await call_next(request)
            sess_exp = _sessions.get(token)
            if sess_exp and sess_exp > _t.monotonic():
                return await call_next(request)
            return JSONResponse(
                {'error': 'Invalid or missing token'},
                status_code=401,
            )

        # ── C-04: Rate limiting middleware ────────────────────────────────────
        @app.middleware('http')
        async def rate_limit_middleware(request: Request, call_next):
            client_ip = request.client.host if request.client else 'unknown'
            now = _t.monotonic()
            # Purge old timestamps
            _rate_buckets[client_ip] = [
                ts for ts in _rate_buckets[client_ip] if now - ts < _RATE_WINDOW
            ]
            if len(_rate_buckets[client_ip]) >= _RATE_LIMIT:
                return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
            _rate_buckets[client_ip].append(now)
            return await call_next(request)

        # ── C-15: Bearer token auth ───────────────────────────────────────────
        _security_scheme = HTTPBearer(auto_error=False)

        def _load_api_key_from_settings() -> str:
            settings_path = _Path.home() / '.automatica' / 'settings.json'
            try:
                cfg = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
                stored = cfg.get('api_key', '')
                if stored:
                    try:
                        from log_filter import SecretFilter as _unused  # noqa
                        import importlib as _il
                        _sm = _il.import_module('secrets')
                        return _sm.decrypt(stored) if hasattr(_sm, 'decrypt') else stored
                    except Exception:
                        return stored
                return stored
            except Exception:
                return ''

        def _verify_token(
            credentials: HTTPAuthorizationCredentials = Security(_security_scheme),
        ):
            """C-15: Verify Bearer token. Allows all if no key configured."""
            expected = os.environ.get('AUTOMATICA_API_KEY', '') or _load_api_key_from_settings()
            if not expected:
                return  # No auth configured — open server
            # C-16: also accept session tokens
            if credentials and credentials.credentials in _sessions:
                if _sessions[credentials.credentials] > _t.monotonic():
                    return  # valid session token
                else:
                    _sessions.pop(credentials.credentials, None)
            if not credentials or credentials.credentials != expected:
                raise HTTPException(status_code=401, detail='Invalid or missing token')

        # ── App state initialisation ─────────────────────────────────────────
        app.state.log_queue: asyncio.Queue = asyncio.Queue()
        app.state.status_queue: asyncio.Queue = asyncio.Queue()
        app.state._ws_clients: set = set()

        async def _broadcast(msg: dict):
            """Push msg to all connected WebSocket clients."""
            await app.state.log_queue.put(msg)
            await app.state.status_queue.put(msg)

        app.state.broadcast = _broadcast

        def push_event(event_type: str, data: dict):
            """
            Thread-safe synchronous helper to push a WS event from any thread.

            Supported event types:
                task_planning      — AI planning pass completed for a task
                subtask_created    — a subtask was created under a parent task
                subtask_completed  — a subtask was marked done
                sprint_started     — a sprint execution began
                sprint_completed   — a sprint execution finished
                task_started       — a task execution began
                task_completed     — a task execution finished

            Usage (from any thread):
                app.state.push_event('subtask_created', {'subtask_id': 'SUB-001'})
            """
            loop = getattr(app.state, 'loop', None)
            if loop is None or not loop.is_running():
                return
            msg = {'type': event_type, **data}
            asyncio.run_coroutine_threadsafe(_broadcast(msg), loop)

        app.state.push_event = push_event

        # ── Helper: global DB connection ─────────────────────────────────────
        def _get_global_conn():
            import automatica_db as adb
            db_path = _Path.home() / '.automatica' / 'automatica.db'
            return adb.init_global_db(db_path)

        # ── Settings (api_key) ───────────────────────────────────────────────
        def _get_settings() -> dict:
            from security_utils import validate_settings as _vs
            settings_path = _Path.home() / '.automatica' / 'settings.json'
            if settings_path.exists():
                try:
                    raw = _json.loads(settings_path.read_text())
                    return _vs(raw)
                except Exception:
                    pass
            return {}

        # ── C-09: Audit log helper ────────────────────────────────────────────
        def _log_audit(conn, action: str, resource: str = '',
                       details: str = '', actor: str = 'api', ip: str = ''):
            try:
                conn.execute(
                    """INSERT INTO audit_log
                       (action, actor, resource, details, ip_address)
                       VALUES (?, ?, ?, ?, ?)""",
                    (action, actor, resource, details, ip),
                )
                conn.commit()
            except Exception:
                pass  # audit log failure must never break the main path

        # ── Auth helper ──────────────────────────────────────────────────────
        def _check_ws_auth(websocket: WebSocket) -> bool:
            expected = os.environ.get('AUTOMATICA_API_KEY', '') or _load_api_key_from_settings()
            if not expected:
                # No key configured: only allow loopback callers (startup
                # gate already prevents non-loopback bind without a key,
                # but defence-in-depth here too).
                client_host = (websocket.client.host if websocket.client else '')
                return client_host in ('127.0.0.1', '::1', 'localhost')
            # Accept Bearer header, X-API-Key header, OR ?token= query param.
            # Browser WebSocket clients can't set custom headers, so ?token=
            # is the only option for them.
            token = ''
            auth_header = websocket.headers.get('authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
            if not token:
                token = (websocket.headers.get('x-api-key', '')
                         or websocket.query_params.get('token', ''))
            if not token:
                return False
            if token == expected:
                return True
            # C-16: accept session tokens
            sess_exp = _sessions.get(token)
            if sess_exp and sess_exp > _t.monotonic():
                return True
            return False

        # ── C-16: POST /api/auth/login ────────────────────────────────────────
        @app.post('/api/auth/login')
        def login(body: dict):
            """Issue a session token valid for SESSION_TTL seconds."""
            expected = os.environ.get('AUTOMATICA_API_KEY', '') or _load_api_key_from_settings()
            if expected and body.get('api_key') != expected:
                raise HTTPException(status_code=401, detail='Invalid API key')
            token = _secrets.token_hex(32)
            _sessions[token] = _t.monotonic() + SESSION_TTL
            return {'token': token, 'expires_in': SESSION_TTL}

        # ── C-16: POST /api/auth/logout ───────────────────────────────────────
        @app.post('/api/auth/logout')
        def logout(credentials: HTTPAuthorizationCredentials = Security(_security_scheme)):
            if credentials:
                _sessions.pop(credentials.credentials, None)
            return {'success': True}

        # ── H2-02: GET /api/auth/qr ───────────────────────────────────────────
        @app.get('/api/auth/qr')
        async def get_auth_qr(request: Request):
            """H2-02: Generate a pairing token and return it as a QR-encodable string."""
            token = _secrets.token_urlsafe(32)
            # Store pending token (in-memory; persisted when device claims it)
            _pending_tokens[token] = {'created': __import__('time').time()}
            pair_url = f"automatica://pair?token={token}&host={request.base_url.hostname}&port=8765"
            try:
                import qrcode, io, base64
                qr = qrcode.make(pair_url)
                buf = io.BytesIO()
                qr.save(buf, format='PNG')
                qr_b64 = base64.b64encode(buf.getvalue()).decode()
                return JSONResponse({'token': token, 'pair_url': pair_url, 'qr_png_b64': qr_b64})
            except ImportError:
                return JSONResponse({'token': token, 'pair_url': pair_url, 'qr_png_b64': None})

        # ── H2-10/H2-13: POST /api/auth/pair ─────────────────────────────────
        @app.post('/api/auth/pair')
        async def pair_device(request: Request):
            """H2-10/H2-13: Client claims a pending token and registers the device."""
            body = await request.json()
            token = body.get('token', '')
            device_id = body.get('device_id', _secrets.token_hex(8))
            device_name = body.get('device_name', 'Unknown')
            platform = body.get('platform', 'unknown')
            if token not in _pending_tokens:
                return JSONResponse({'error': 'Invalid or expired token'}, status_code=403)
            del _pending_tokens[token]
            try:
                import automatica_db as adb
                global_conn = _get_global_conn()
                adb.register_device(global_conn, device_id, device_name, platform, token)
                adb.log_auth_event(global_conn, 'pair', device_id, device_name,
                                   request.client.host if request.client else '')
            except Exception as exc:
                print(f"[auth] pair error: {exc}")
            return JSONResponse({'device_id': device_id, 'token': token, 'status': 'paired'})

        # ── H2-09: GET /api/auth/devices ─────────────────────────────────────
        @app.get('/api/auth/devices', dependencies=[Depends(_verify_token)])
        async def list_devices_endpoint():
            """H2-09: List all paired devices."""
            try:
                import automatica_db as adb
                return JSONResponse(adb.list_devices(_get_global_conn()))
            except Exception as exc:
                return JSONResponse({'error': str(exc)}, status_code=500)

        # ── H2-11: DELETE /api/auth/devices/{device_id} ──────────────────────
        @app.delete('/api/auth/devices/{device_id}', dependencies=[Depends(_verify_token)])
        async def revoke_device_endpoint(device_id: str, request: Request):
            """H2-11: Revoke a paired device."""
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                ok = adb.revoke_device(conn, device_id)
                adb.log_auth_event(conn, 'revoke', device_id,
                                   ip_address=request.client.host if request.client else '')
                return JSONResponse({'revoked': ok})
            except Exception as exc:
                return JSONResponse({'error': str(exc)}, status_code=500)

        # ── H2-08: GET /api/auth/audit ────────────────────────────────────────
        @app.get('/api/auth/audit', dependencies=[Depends(_verify_token)])
        async def get_audit_log_endpoint(limit: int = 100):
            """H2-08: Get auth audit log."""
            try:
                import automatica_db as adb
                return JSONResponse(adb.get_auth_audit_log(_get_global_conn(), limit))
            except Exception as exc:
                return JSONResponse({'error': str(exc)}, status_code=500)

        # ── GET /api/status ──────────────────────────────────────────────────
        @app.get('/api/status')
        def status():
            return {
                'status': 'ok',
                'mode': 'server',
                'version': '0.2.0',
            }

        # ── GET /api/wiki/{project_path} ─────────────────────────────────────
        @app.get('/api/wiki/{project_path:path}',
                 dependencies=[Depends(_verify_token)])
        def get_wiki_index(project_path: str):
            """Return the wiki index JSON for a project path. C-07: path traversal prevention."""
            from project_scanner import build_file_wiki
            from security import safe_project_path

            try:
                proj_dir = safe_project_path(project_path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            try:
                wiki_data = build_file_wiki(proj_dir)
                return wiki_data
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f'Failed to build wiki index: {exc}')

        # ── S1-W01: GET /api/projects ────────────────────────────────────────
        @app.get('/api/projects')
        def get_projects():
            """List all known projects from the global DB."""
            try:
                conn = _get_global_conn()
                rows = conn.execute(
                    "SELECT id, name, directory, created_at FROM known_projects"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── S1-W02: GET /api/projects/{project_id}/tasks ─────────────────────
        @app.get('/api/projects/{project_id}/tasks')
        def get_tasks(project_id: int):
            """Return tasks for the given project from its project DB."""
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                row = conn.execute(
                    "SELECT directory FROM known_projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f'Project {project_id} not found')
                project_dir = Path(row['directory'])
                pdb = adb.init_project_db(project_dir)
                rows = pdb.execute(
                    """SELECT id, display_id, title, status, complexity, created_at
                       FROM tasks ORDER BY sort_order"""
                ).fetchall()
                return [dict(r) for r in rows]
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── S1-W03: POST /api/projects/{project_id}/tasks ────────────────────
        @app.post('/api/projects/{project_id}/tasks')
        def create_task(project_id: int, body: dict):
            """Create a new task in the project DB."""
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                row = conn.execute(
                    "SELECT directory FROM known_projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f'Project {project_id} not found')
                project_dir = Path(row['directory'])
                pdb = adb.init_project_db(project_dir)
                result = adb.create_task(
                    pdb,
                    title=body.get('title', ''),
                    description=body.get('description', ''),
                    complexity=int(body.get('complexity', 3)),
                )
                return {'success': True, 'task_id': result['id'], 'display_id': result['display_id']}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── D-20: POST /api/tasks/generate ────────────────────────────────────
        @app.post('/api/tasks/generate')
        def generate_tasks(body: dict):
            """Generate task suggestions from a project goal description.

            Uses a simple heuristic (sentence-split) to produce 5-10 task titles.
            Returns list of {title, complexity}.
            """
            goal = (body.get('goal') or '').strip()
            if not goal:
                raise HTTPException(status_code=400, detail='goal is required')
            try:
                import re as _re
                # Try Ollama if available for richer generation
                tasks_out = []
                try:
                    from ollama_utils import query_ollama
                    prompt = (
                        f"Given this project goal: {goal}\n\n"
                        "List 5-10 concrete implementation tasks as a numbered list. "
                        "Format each as: 1. Task title (complexity: N)\n"
                        "Complexity should be one of: 1, 2, 3, 5, 8, 13"
                    )
                    response = query_ollama(prompt, timeout=10)
                    if response:
                        for line in response.splitlines():
                            m = _re.match(r'\d+\.\s+(.+?)(?:\s+\(complexity:\s*(\d+)\))?$',
                                          line.strip(), _re.IGNORECASE)
                            if m:
                                title = m.group(1).strip()
                                cx = int(m.group(2)) if m.group(2) else 3
                                if cx not in (1, 2, 3, 5, 8, 13):
                                    cx = 3
                                tasks_out.append({'title': title, 'complexity': cx})
                except Exception:
                    pass

                if not tasks_out:
                    # Heuristic fallback: split on sentence boundaries
                    sentences = _re.split(r'[.;,\n]+', goal)
                    for s in sentences:
                        s = s.strip()
                        if len(s) > 5:
                            tasks_out.append({'title': s[:200], 'complexity': 3})
                    tasks_out = tasks_out[:10]

                return {'tasks': tasks_out}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── F-04: POST /api/tasks/commit-message ─────────────────────────────
        @app.post('/api/tasks/commit-message')
        def generate_commit_message(body: dict):
            """Generate a commit message from dirty files and completed tasks."""
            project_id = body.get('project_id')
            dirty_files = body.get('dirty_files', [])
            completed_tasks = body.get('completed_tasks', [])
            try:
                # Build message from completed tasks if available
                if completed_tasks:
                    titles = [t.get('title', str(t)) if isinstance(t, dict) else str(t)
                              for t in completed_tasks[:3]]
                    if len(titles) == 1:
                        msg = f"feat: complete {titles[0]}"
                    else:
                        msg = f"feat: complete {', '.join(titles[:2])}"
                        if len(completed_tasks) > 2:
                            msg += f' and {len(completed_tasks) - 2} more tasks'
                elif dirty_files:
                    short = ', '.join(dirty_files[:3])
                    msg = f'feat: update {short}'
                    if len(dirty_files) > 3:
                        msg += f' and {len(dirty_files) - 3} more files'
                else:
                    msg = 'feat: update project files'
                return {'message': msg, 'success': True}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── S1-W04: POST /api/sprints/start ──────────────────────────────────
        @app.post('/api/sprints/start')
        def start_sprint(body: dict):
            """Launch SprintRunner in a background thread for the given project.

            Body fields:
              project_id: int (required)
              provider: str (default 'default')
              dry_run: bool (WP-5.4 — validate pipeline without executing agents)
              triggered_by: str (optional — 'on_schedule', 'on_commit', 'manual')
            """
            project_id = body.get('project_id')
            if project_id is None:
                raise HTTPException(status_code=400, detail='project_id required')
            dry_run = body.get('dry_run', False)
            try:
                import automatica_db as adb
                from sprint_runner import SprintRunner

                conn = _get_global_conn()
                row = conn.execute(
                    "SELECT directory, name FROM known_projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f'Project {project_id} not found')

                project_dir = Path(row['directory'])
                sprint_id = str(uuid.uuid4())

                if dry_run:
                    # WP-5.4: dry-run — load pipeline config, validate stages, return report
                    import json as _j
                    stages_file = project_dir / 'automatica' / 'pipeline_stages.json'
                    stages = []
                    if stages_file.exists():
                        try:
                            stages = _j.loads(stages_file.read_text())
                        except Exception:
                            pass
                    return {
                        'success': True,
                        'dry_run': True,
                        'sprint_id': sprint_id,
                        'stages': stages,
                        'stage_count': len(stages),
                        'enabled_stages': [s['name'] for s in stages if s.get('enabled', True)],
                        'message': f'Dry run: {len(stages)} stages validated for project {row["name"]}',
                    }

                def _run_sprint():
                    try:
                        runner = SprintRunner(
                            project_dir=project_dir,
                            provider=body.get('provider', 'default'),
                            provider_config={},
                        )
                        sprint_res = runner.run_sprint()
                        # F-08: Auto-push if enabled in settings
                        try:
                            import json as _j
                            _cfg = _j.loads(settings_path.read_text()) if settings_path.exists() else {}
                            if _cfg.get('git_auto_push', False):
                                _remote = _cfg.get('git_remote', 'origin')
                                import subprocess as _sp
                                _r = _sp.run(
                                    ['git', 'push', _remote],
                                    cwd=str(project_dir),
                                    capture_output=True, timeout=30,
                                )
                                if _r.returncode == 0:
                                    print(f'[API SERVER] Auto-pushed to {_remote}')
                                else:
                                    print(f'[API SERVER] Auto-push failed: {_r.stderr.strip()[:200]}')
                        except Exception as _push_exc:
                            print(f'[API SERVER] Auto-push error: {_push_exc}')
                    except Exception as exc:
                        print(f'[API SERVER] Sprint error: {exc}')

                t = threading.Thread(target=_run_sprint, daemon=True,
                                     name=f'sprint-{sprint_id[:8]}')
                t.start()

                return {
                    'success': True,
                    'sprint_id': sprint_id,
                    'message': f'Sprint started for project {row["name"]}',
                }
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── WP-3.9: POST /api/sprints/{sprint_id}/cancel ─────────────────────
        @app.post('/api/sprints/{sprint_id}/cancel')
        def cancel_sprint(sprint_id: str):
            """Request cancellation of a running sprint thread."""
            # Find thread by name convention 'sprint-<first8>'
            import threading as _thr
            prefix = f'sprint-{sprint_id[:8]}'
            found = [t for t in _thr.enumerate() if t.name.startswith(prefix)]
            # Threads can't be forcibly killed in Python; set a cancel flag instead
            if hasattr(app.state, '_cancelled_sprints'):
                app.state._cancelled_sprints.add(sprint_id)
            else:
                app.state._cancelled_sprints = {sprint_id}
            return {
                'success': True,
                'sprint_id': sprint_id,
                'running_threads': len(found),
                'message': 'Cancel requested — sprint will stop after current task.',
            }

        # ── WP-4.15: POST /api/triggers/commit ───────────────────────────────
        @app.post('/api/triggers/commit')
        def on_commit_trigger(body: dict):
            """on_commit webhook: GitHub/GitLab push event → start sprint for project.

            Expects body: {'project_id': int, 'ref': str, 'commits': list}
            Fires only if `on_commit` trigger is enabled in project triggers.json.
            """
            project_id = body.get('project_id')
            if project_id is None:
                raise HTTPException(status_code=400, detail='project_id required')

            try:
                import json as _j
                conn = _get_global_conn()
                row = conn.execute(
                    "SELECT directory FROM known_projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f'Project {project_id} not found')

                project_dir = row['directory']
                # Check on_commit trigger enabled
                triggers_file = Path(project_dir) / 'automatica' / 'triggers.json'
                triggers = []
                if triggers_file.exists():
                    triggers = _j.loads(triggers_file.read_text())
                on_commit = next((t for t in triggers if t.get('key') == 'on_commit'), None)
                if not on_commit or not on_commit.get('enabled', False):
                    return {'success': False, 'message': 'on_commit trigger is disabled'}

                # Re-use start_sprint logic
                sprint_id = str(uuid.uuid4())

                def _run():
                    try:
                        from sprint_runner import SprintRunner
                        runner = SprintRunner(project_dir=Path(project_dir),
                                             provider='default', provider_config={})
                        runner.run_sprint()
                    except Exception as exc:
                        print(f'[on_commit] Sprint error: {exc}')

                threading.Thread(target=_run, daemon=True,
                                 name=f'sprint-{sprint_id[:8]}').start()
                return {'success': True, 'sprint_id': sprint_id,
                        'message': 'Sprint triggered by commit webhook'}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── S1-W05: GET /api/logs ─────────────────────────────────────────────
        @app.get('/api/logs')
        def get_logs(limit: int = 200, project_id: Optional[int] = None):
            """Return action log entries, optionally filtered by project."""
            try:
                import automatica_db as adb

                if project_id is not None:
                    conn = _get_global_conn()
                    row = conn.execute(
                        "SELECT directory FROM known_projects WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail=f'Project {project_id} not found')
                    pdb = adb.init_project_db(Path(row['directory']))
                else:
                    # Use first known project if no project_id given
                    conn = _get_global_conn()
                    first = conn.execute(
                        "SELECT directory FROM known_projects LIMIT 1"
                    ).fetchone()
                    if not first:
                        return []
                    pdb = adb.init_project_db(Path(first['directory']))

                rows = pdb.execute(
                    """SELECT al.id, al.scope_id AS task_id, al.actor,
                              al.action, al.detail, al.session_id,
                              al.timestamp AS created_at
                       FROM action_log al
                       ORDER BY al.id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── S3-W11: GET /api/providers ───────────────────────────────────────
        @app.get('/api/providers')
        def list_providers():
            """List all configured AI providers from the global DB."""
            try:
                conn = _get_global_conn()
                rows = conn.execute(
                    "SELECT id, name, provider_type, base_url, model_name, enabled, "
                    "       api_key_env, max_tokens, created_at "
                    "FROM providers ORDER BY id"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── GET /api/providers/discover ──────────────────────────────────────
        # Read-only probe: scans PATH for known CLIs and pings known local
        # HTTP endpoints (Ollama, etc.) and returns suggested rows the UI
        # can preview before persisting. Persisting goes through the existing
        # authenticated POST /api/providers endpoint, so this endpoint is
        # safe to expose without state-changing auth — it modifies nothing.
        @app.get('/api/providers/discover')
        def discover_providers():
            """Auto-detect available AI providers on the host (read-only)."""
            try:
                from adapters.discovery import discover_all
                discovered = discover_all()
                # Cross-reference with already-registered providers so the UI
                # can mark which discoveries are new vs. already in the DB.
                conn = _get_global_conn()
                existing = {
                    row['name']
                    for row in conn.execute(
                        "SELECT name FROM providers"
                    ).fetchall()
                }
                for row in discovered:
                    row['already_registered'] = row['name'] in existing
                return {"discovered": discovered, "registered_names": sorted(existing)}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-01: GET /api/agents ─────────────────────────────────────────────
        @app.get('/api/agents')
        def list_agents():
            """List all agent definitions from the global DB."""
            try:
                conn = _get_global_conn()
                rows = conn.execute(
                    "SELECT slug, role, description, created_at FROM agent_definitions"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-01: POST /api/agents ────────────────────────────────────────────
        @app.post('/api/agents', dependencies=[Depends(_verify_token)])
        def create_agent(body: 'AgentCreate', request: Request):
            """Insert a new agent definition into the global DB."""
            try:
                from security import _sanitize_slug
                slug = _sanitize_slug(body.slug)
                conn = _get_global_conn()
                conn.execute(
                    """INSERT INTO agent_definitions
                       (slug, display_name, role, description, system_prompt)
                       VALUES (?, ?, ?, ?, ?)""",
                    (slug, slug, body.role, body.description, body.prompt),
                )
                conn.commit()
                # C-09: audit log
                client_ip = request.client.host if request.client else ''
                _log_audit(conn, 'create_agent', f'agent:{slug}', actor='api', ip=client_ip)
                return {'success': True, 'slug': slug}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-02: DELETE /api/agents/{slug} ──────────────────────────────────
        @app.delete('/api/agents/{slug}', dependencies=[Depends(_verify_token)])
        def delete_agent(slug: str, request: Request):
            """Remove an agent definition by slug."""
            try:
                conn = _get_global_conn()
                conn.execute("DELETE FROM agent_definitions WHERE slug=?", (slug,))
                conn.commit()
                # C-09: audit log
                client_ip = request.client.host if request.client else ''
                _log_audit(conn, 'delete_agent', f'agent:{slug}', actor='api', ip=client_ip)
                return {'success': True}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-03: GET /api/sprints ────────────────────────────────────────────
        @app.get('/api/sprints')
        def list_sprints(project_id: Optional[int] = None):
            """List sprints, optionally filtered by project."""
            try:
                import automatica_db as adb

                if project_id is not None:
                    conn = _get_global_conn()
                    row = conn.execute(
                        "SELECT directory FROM known_projects WHERE id=?",
                        (project_id,),
                    ).fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail=f'Project {project_id} not found')
                    pdb = adb.init_project_db(Path(row['directory']))
                    rows = pdb.execute(
                        """SELECT id, sprint_number, status, goal, created_at
                           FROM sprints ORDER BY sprint_number DESC"""
                    ).fetchall()
                    return [dict(r) for r in rows]
                else:
                    # No project: return empty list
                    return []
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-03: POST /api/sprints ───────────────────────────────────────────
        @app.post('/api/sprints')
        def create_sprint(body: 'SprintCreate'):
            """Create a new sprint in the given project DB."""
            try:
                import automatica_db as adb

                conn = _get_global_conn()
                row = conn.execute(
                    "SELECT directory FROM known_projects WHERE id=?",
                    (body.project_id,),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f'Project {body.project_id} not found')
                pdb = adb.init_project_db(Path(row['directory']))
                # Determine next sprint number
                last = pdb.execute("SELECT MAX(sprint_number) FROM sprints").fetchone()
                next_num = (last[0] or 0) + 1
                pdb.execute(
                    "INSERT INTO sprints (sprint_number, status, goal) VALUES (?, 'planning', ?)",
                    (next_num, body.goal),
                )
                pdb.commit()
                sprint_id = pdb.execute("SELECT last_insert_rowid()").fetchone()[0]
                return {'success': True, 'id': sprint_id, 'sprint_number': next_num}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-04: PATCH /api/tasks/{task_id} ─────────────────────────────────
        @app.patch('/api/tasks/{task_id}')
        def update_task(task_id: int, body: 'TaskUpdate'):
            """Update allowed fields on a task (looks up project by task_id)."""
            try:
                import automatica_db as adb

                conn = _get_global_conn()
                projects = conn.execute(
                    "SELECT id, directory FROM known_projects"
                ).fetchall()
                pdb = None
                for proj in projects:
                    try:
                        _pdb = adb.init_project_db(Path(proj['directory']))
                        hit = _pdb.execute(
                            "SELECT id FROM tasks WHERE id=?", (task_id,)
                        ).fetchone()
                        if hit:
                            pdb = _pdb
                            break
                    except Exception:
                        continue
                if pdb is None:
                    raise HTTPException(status_code=404, detail=f'Task {task_id} not found')

                _TASK_SAFE_COLS = {
                    'title': 'title', 'description': 'description',
                    'status': 'status', 'complexity': 'complexity',
                }
                fields = body.model_dump(exclude_none=True)
                for k, v in fields.items():
                    col = _TASK_SAFE_COLS.get(k)
                    if col:
                        pdb.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (v, task_id))
                pdb.commit()
                return {'success': True}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-04: DELETE /api/tasks/{task_id} ────────────────────────────────
        @app.delete('/api/tasks/{task_id}')
        def delete_task(task_id: int):
            """Mark a task as deleted (soft delete)."""
            try:
                import automatica_db as adb

                conn = _get_global_conn()
                projects = conn.execute(
                    "SELECT id, directory FROM known_projects"
                ).fetchall()
                pdb = None
                for proj in projects:
                    try:
                        _pdb = adb.init_project_db(Path(proj['directory']))
                        hit = _pdb.execute(
                            "SELECT id FROM tasks WHERE id=?", (task_id,)
                        ).fetchone()
                        if hit:
                            pdb = _pdb
                            break
                    except Exception:
                        continue
                if pdb is None:
                    raise HTTPException(status_code=404, detail=f'Task {task_id} not found')

                pdb.execute("UPDATE tasks SET status='archived' WHERE id=?", (task_id,))
                pdb.commit()
                return {'success': True}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-05: PATCH /api/providers/{provider_id} ─────────────────────────
        @app.patch('/api/providers/{provider_id}')
        def update_provider(provider_id: int, body: dict):
            """Update allowed fields on a provider."""
            try:
                conn = _get_global_conn()
                # Explicit safe-column map; never interpolate user input as column name
                _PROVIDER_SAFE_COLS = {
                    'name': 'name', 'base_url': 'base_url', 'model_name': 'model_name',
                    'provider_type': 'provider_type', 'enabled': 'enabled',
                    'url': 'base_url', 'model': 'model_name', 'type': 'provider_type',
                }
                for k, v in body.items():
                    col = _PROVIDER_SAFE_COLS.get(k)
                    if col:
                        conn.execute(f"UPDATE providers SET {col}=? WHERE id=?", (v, provider_id))
                conn.commit()
                return {'success': True}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-05: DELETE /api/providers/{provider_id} ─────────────────────────
        @app.delete('/api/providers/{provider_id}', dependencies=[Depends(_verify_token)])
        def delete_provider(provider_id: int, request: Request):
            """Remove a provider by ID."""
            try:
                conn = _get_global_conn()
                conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))
                conn.commit()
                # C-09: audit log
                client_ip = request.client.host if request.client else ''
                _log_audit(conn, 'delete_provider', f'provider:{provider_id}',
                           actor='api', ip=client_ip)
                return {'success': True}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── B-06: GET /api/standards ──────────────────────────────────────────
        @app.get('/api/standards')
        def list_standards():
            """Return all standards markdown files from the standards directory."""
            import json as _json
            settings = _get_settings()
            std_dir_path = settings.get('standards_dir', '')
            std_dir = Path(std_dir_path) if std_dir_path else Path.home() / '.automatica' / 'standards'
            if not std_dir.exists():
                return []
            result = []
            for f in sorted(std_dir.glob('*.md')):
                try:
                    result.append({
                        'name': f.stem,
                        'filename': f.name,
                        'content': f.read_text(encoding='utf-8'),
                        'path': str(f),
                    })
                except Exception:
                    pass
            return result

        # ── B-06: POST /api/standards ─────────────────────────────────────────
        @app.post('/api/standards')
        def save_standard(body: 'StandardSave'):
            """Write a standards markdown file."""
            settings = _get_settings()
            std_dir_path = settings.get('standards_dir', '')
            std_dir = Path(std_dir_path) if std_dir_path else Path.home() / '.automatica' / 'standards'
            std_dir.mkdir(parents=True, exist_ok=True)
            # Sanitise name: keep only safe filename chars
            safe_name = ''.join(c for c in body.name if c.isalnum() or c in ' _-.')
            filename = safe_name if safe_name.endswith('.md') else f'{safe_name}.md'
            p = std_dir / filename
            p.write_text(body.content, encoding='utf-8')
            return {'success': True, 'path': str(p)}

        # ── B-07: GET /api/settings ───────────────────────────────────────────
        @app.get('/api/settings', dependencies=[Depends(_verify_token)])
        def get_settings_route():
            """Return non-sensitive settings.json contents."""
            _SENSITIVE = {'api_key', 'smtp_password', 'ssl_certfile', 'ssl_keyfile', 'secret'}
            return {k: v for k, v in _get_settings().items() if k not in _SENSITIVE}

        # ── B-07: POST /api/settings ──────────────────────────────────────────
        @app.post('/api/settings')
        def save_settings_route(body: dict):
            """Merge body dict into settings.json and persist."""
            import json as _json
            settings_path = Path.home() / '.automatica' / 'settings.json'
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            existing = _get_settings()
            existing.update(body)
            settings_path.write_text(_json.dumps(existing, indent=2))
            return {'success': True}

        # ── B-20: GET /api/health ─────────────────────────────────────────────
        @app.get('/api/health')
        def health_check():
            """Detailed health check endpoint."""
            import platform
            import sys as _sys

            db_ok = False
            try:
                conn = _get_global_conn()
                conn.execute("SELECT 1").fetchone()
                db_ok = True
            except Exception:
                db_ok = False

            return {
                'status': 'ok' if db_ok else 'degraded',
                'version': '1.0.0',
                'python': _sys.version,
                'platform': platform.platform(),
                'db': 'ok' if db_ok else 'error',
                'uptime_s': int(_time.monotonic()),
            }

        # ── S3-W11: POST /api/providers ──────────────────────────────────────
        @app.post('/api/providers', dependencies=[Depends(_verify_token)])
        def add_provider_route(body: dict, request: Request):
            """Add an AI provider to the global DB."""
            name = body.get('name', '').strip()
            if not name:
                raise HTTPException(status_code=400, detail='name is required')
            provider_type = body.get('provider_type', body.get('type', 'api')).strip()
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                result = adb.add_provider(
                    conn,
                    name=name,
                    provider_type=provider_type,
                    base_url=body.get('base_url', body.get('url', '')),
                    model_name=body.get('model_name', body.get('model', '')),
                    api_key_env=body.get('api_key_env', body.get('key_env', '')),
                    max_tokens=int(body.get('max_tokens', 4096)),
                )
                # C-09: audit log
                client_ip = request.client.host if request.client else ''
                _log_audit(conn, 'create_provider', f'provider:{name}',
                           actor='api', ip=client_ip)
                return {'success': True, 'id': result.get('id'), 'name': result.get('name')}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # ── E-22: POST /api/agents/{slug}/run ────────────────────────────────
        @app.post('/api/agents/{slug}/run', dependencies=[Depends(_verify_token)])
        def run_agent_on_task(slug: str, body: dict):
            """Launch a single agent run on a task in a background thread."""
            task_display_id = body.get('task_display_id')
            project_id = body.get('project_id')
            if not task_display_id:
                raise HTTPException(status_code=400, detail='task_display_id required')

            run_id = str(uuid.uuid4())

            def _run():
                try:
                    import automatica_db as adb
                    gconn = _get_global_conn()
                    # resolve project
                    if project_id:
                        proj = gconn.execute(
                            "SELECT directory FROM known_projects WHERE id=?", (project_id,)
                        ).fetchone()
                    else:
                        proj = gconn.execute(
                            "SELECT directory FROM known_projects LIMIT 1"
                        ).fetchone()
                    if not proj:
                        print(f'[run_agent] No project found for id={project_id}')
                        return

                    # Resolve provider config from agent definition
                    agent_row = gconn.execute(
                        "SELECT provider_id FROM agent_definitions WHERE slug=?", (slug,)
                    ).fetchone()
                    provider_cfg = {}
                    provider_name = 'default'
                    if agent_row and agent_row['provider_id']:
                        prov = gconn.execute(
                            "SELECT name, provider_type, base_url, model_name "
                            "FROM providers WHERE id=?", (agent_row['provider_id'],)
                        ).fetchone()
                        if prov:
                            provider_name = prov['name']
                            provider_cfg = {
                                'endpoint': prov['base_url'] or '',
                                'model': prov['model_name'] or '',
                            }

                    from sprint_runner import SprintRunner
                    runner = SprintRunner(
                        project_dir=proj['directory'],
                        provider=provider_name,
                        provider_config=provider_cfg,
                    )
                    result = runner.run_single_task(task_display_id)
                    print(f'[run_agent] {slug} on {task_display_id}: '
                          f'{"OK" if result.success else "FAIL"} in {result.duration:.1f}s')
                except Exception as exc:
                    print(f'[run_agent] Error: {exc}')

            threading.Thread(target=_run, daemon=True,
                             name=f'agent-run-{run_id[:8]}').start()
            return {
                'success': True,
                'run_id': run_id,
                'message': f'Agent {slug!r} started on task {task_display_id!r}',
            }

        # ── F-17: POST /api/webhooks/receive ─────────────────────────────────
        @app.post('/api/webhooks/receive')
        async def receive_webhook(request: Request):
            """Receive external webhook triggers (e.g., from GitHub CI)."""
            body = await request.json()
            event_type = body.get('event', 'unknown')
            # Push to WS log queue
            if hasattr(app.state, 'push_event'):
                app.state.push_event('webhook_received', {'event_type': event_type, 'data': body})
            return {'success': True, 'event': event_type}

        # ── F-18: GET /api/openapi.json redirect ──────────────────────────────
        from fastapi.responses import RedirectResponse as _RR

        @app.get('/api/openapi.json')
        async def openapi_redirect():
            """Redirect /api/openapi.json to FastAPI's generated spec at /openapi.json."""
            return _RR(url='/openapi.json')

        # ── S1-W06: WS /ws/logs ───────────────────────────────────────────────
        @app.websocket('/ws/logs')
        async def ws_logs(websocket: WebSocket):
            """Real-time log stream. Sends last 50 entries on connect then pushes new ones."""
            if not _check_ws_auth(websocket):
                await websocket.close(code=1008)
                return

            await websocket.accept()
            app.state._ws_clients.add(websocket)

            # Send last 50 log entries on connect
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                first = conn.execute(
                    "SELECT directory FROM known_projects LIMIT 1"
                ).fetchone()
                if first:
                    pdb = adb.init_project_db(Path(first['directory']))
                    rows = pdb.execute(
                        """SELECT id, scope_id AS task_id, actor, action,
                                  detail, session_id, timestamp AS created_at
                           FROM action_log ORDER BY id DESC LIMIT 50"""
                    ).fetchall()
                    import json
                    for r in reversed(rows):
                        await websocket.send_text(json.dumps(dict(r)))
            except Exception:
                pass

            # Keep connection alive and push new entries from the queue
            import json
            queue: asyncio.Queue = app.state.log_queue
            _WS_ALLOWED_KEYS = {'type', 'data', 'id'}
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=30)
                        await websocket.send_text(json.dumps(msg))
                    except asyncio.TimeoutError:
                        # C-13: validate any incoming client messages
                        try:
                            raw_msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
                            try:
                                client_msg = json.loads(raw_msg)
                                if not isinstance(client_msg, dict):
                                    raise ValueError('Message must be a JSON object')
                                unknown = set(client_msg.keys()) - _WS_ALLOWED_KEYS
                                if unknown:
                                    raise ValueError(f'Unknown keys: {unknown}')
                            except (json.JSONDecodeError, ValueError) as e:
                                await websocket.send_json({'error': str(e)})
                        except asyncio.TimeoutError:
                            pass  # no client message — send keepalive
                        await websocket.send_text(json.dumps({'type': 'ping'}))
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                app.state._ws_clients.discard(websocket)

        # ── S1-W07: WS /ws/status ─────────────────────────────────────────────
        @app.websocket('/ws/status')
        async def ws_status(websocket: WebSocket):
            """Live sprint/task status updates."""
            if not _check_ws_auth(websocket):
                await websocket.close(code=1008)
                return

            await websocket.accept()
            app.state._ws_clients.add(websocket)

            import json
            queue: asyncio.Queue = app.state.status_queue
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=30)
                        await websocket.send_text(json.dumps(msg))
                    except asyncio.TimeoutError:
                        await websocket.send_text(json.dumps({'type': 'ping'}))
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                app.state._ws_clients.discard(websocket)

        # ── WP-6.4: Quality API endpoints ─────────────────────────────────────

        def _get_project_path(project_id: int) -> Optional[str]:
            """Look up project directory from global DB by project_id."""
            try:
                import automatica_db as adb
                conn = _get_global_conn()
                row = conn.execute(
                    'SELECT directory FROM known_projects WHERE id=?', (project_id,)
                ).fetchone()
                return row[0] if row else None
            except Exception:
                return None

        def _resolve_quality_path(project_id: Optional[int], project_path: Optional[str]) -> Optional[str]:
            """C-07: Resolve and validate a project path for quality endpoints.
            If project_path is user-supplied, validate it exists as a real directory.
            If project_id is supplied, look up from DB (trusted).
            """
            if project_path:
                try:
                    from security import safe_project_path as _safe_pp
                    return str(_safe_pp(project_path))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f'Invalid project path: {exc}')
                except ImportError:
                    p = Path(project_path).resolve()
                    if not p.exists() or not p.is_dir():
                        raise HTTPException(status_code=400, detail='Invalid project path')
                    return str(p)
            if project_id:
                return _get_project_path(project_id)
            return None

        def _run_subprocess(cmd: list, cwd: str, timeout: int = 60) -> dict:
            """Run a subprocess and return {stdout, stderr, returncode}."""
            import subprocess
            try:
                result = subprocess.run(
                    cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
                )
                return {
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'returncode': result.returncode,
                }
            except subprocess.TimeoutExpired:
                return {'stdout': '', 'stderr': 'Timed out', 'returncode': -1}
            except FileNotFoundError as exc:
                return {'stdout': '', 'stderr': str(exc), 'returncode': -2}
            except Exception as exc:
                return {'stdout': '', 'stderr': str(exc), 'returncode': -3}

        @app.get('/api/quality/tests')
        async def quality_tests(project_id: Optional[int] = None, project_path: Optional[str] = None):
            """WP-6.4: Run pytest in project directory. Returns parsed results."""
            import json as _j
            loop = asyncio.get_event_loop()
            path = _resolve_quality_path(project_id, project_path)
            if not path or not Path(path).exists():
                return {'error': 'Project path not found', 'project_id': project_id}

            # Run pytest with JSON report if pytest-json-report installed, else plain
            def _run():
                # Try JSON report first
                import tempfile, os
                tmp = tempfile.mktemp(suffix='.json')
                r = _run_subprocess(
                    ['python', '-m', 'pytest', '--tb=short', f'--json-report',
                     f'--json-report-file={tmp}', '-q'],
                    cwd=path, timeout=120,
                )
                if r['returncode'] != -2 and Path(tmp).exists():
                    try:
                        data = _j.loads(Path(tmp).read_text())
                        os.unlink(tmp)
                        return {
                            'status': 'ok',
                            'passed': data.get('summary', {}).get('passed', 0),
                            'failed': data.get('summary', {}).get('failed', 0),
                            'errors': data.get('summary', {}).get('error', 0),
                            'duration': data.get('duration', 0),
                            'raw': data.get('summary', {}),
                        }
                    except Exception:
                        pass
                # Fallback: parse text output
                lines = r['stdout'].splitlines() + r['stderr'].splitlines()
                summary = next((l for l in reversed(lines) if 'passed' in l or 'failed' in l or 'error' in l), '')
                return {
                    'status': 'ok' if r['returncode'] == 0 else 'failed',
                    'summary': summary,
                    'output': r['stdout'][-3000:],
                    'returncode': r['returncode'],
                }

            result = await loop.run_in_executor(None, _run)
            return result

        @app.get('/api/quality/lint')
        async def quality_lint(project_id: Optional[int] = None, project_path: Optional[str] = None,
                               tool: str = 'flake8'):
            """WP-6.4: Run flake8 or ruff on project. Returns list of issues."""
            loop = asyncio.get_event_loop()
            path = _resolve_quality_path(project_id, project_path)
            if not path or not Path(path).exists():
                return {'error': 'Project path not found'}

            def _run():
                if tool == 'ruff':
                    r = _run_subprocess(
                        ['python', '-m', 'ruff', 'check', '.', '--output-format=json'],
                        cwd=path, timeout=60,
                    )
                    try:
                        import json as _j
                        issues = _j.loads(r['stdout']) if r['stdout'].strip() else []
                        return {'tool': 'ruff', 'issue_count': len(issues), 'issues': issues[:200]}
                    except Exception:
                        pass
                # Default: flake8
                r = _run_subprocess(
                    ['python', '-m', 'flake8', '.', '--max-line-length=120',
                     '--format=%(path)s:%(row)d:%(col)d: %(code)s %(text)s'],
                    cwd=path, timeout=60,
                )
                lines = [l for l in r['stdout'].splitlines() if l.strip()]
                return {
                    'tool': 'flake8',
                    'issue_count': len(lines),
                    'issues': lines[:200],
                    'returncode': r['returncode'],
                }

            result = await loop.run_in_executor(None, _run)
            return result

        @app.get('/api/quality/coverage')
        async def quality_coverage(project_id: Optional[int] = None, project_path: Optional[str] = None):
            """WP-6.4: Run pytest --cov and return coverage summary."""
            loop = asyncio.get_event_loop()
            path = _resolve_quality_path(project_id, project_path)
            if not path or not Path(path).exists():
                return {'error': 'Project path not found'}

            def _run():
                import json as _j
                r = _run_subprocess(
                    ['python', '-m', 'pytest', '--cov=.', '--cov-report=json',
                     '--cov-report=term-missing', '-q', '--tb=no'],
                    cwd=path, timeout=120,
                )
                cov_path = Path(path) / 'coverage.json'
                if cov_path.exists():
                    try:
                        data = _j.loads(cov_path.read_text())
                        totals = data.get('totals', {})
                        return {
                            'status': 'ok',
                            'line_rate': totals.get('percent_covered', 0),
                            'lines_covered': totals.get('covered_lines', 0),
                            'lines_total': totals.get('num_statements', 0),
                            'missing': totals.get('missing_lines', 0),
                            'files': {
                                k: {
                                    'line_rate': v.get('summary', {}).get('percent_covered', 0),
                                    'missing': v.get('missing_lines', []),
                                }
                                for k, v in list(data.get('files', {}).items())[:50]
                            },
                        }
                    except Exception:
                        pass
                # Fallback: parse terminal output
                lines = r['stdout'].splitlines()
                total_line = next((l for l in reversed(lines) if 'TOTAL' in l), '')
                return {
                    'status': 'ok' if r['returncode'] == 0 else 'failed',
                    'summary': total_line,
                    'output': r['stdout'][-2000:],
                    'returncode': r['returncode'],
                }

            result = await loop.run_in_executor(None, _run)
            return result

        # ── Triggers CRUD (project-scoped) ───────────────────────────────────
        # Added in support of Automatica-App polish pass: triggers screen now
        # round-trips through the API instead of local-JSON-only persistence.
        @app.get('/api/projects/{project_id}/triggers')
        def list_project_triggers(project_id: int):
            from automatica_db import init_project_db
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            pdir = row['directory']
            pconn = init_project_db(Path(pdir))
            rows = pconn.execute(
                'SELECT id, name, type, config_json, enabled, created_at, updated_at '
                'FROM triggers ORDER BY id'
            ).fetchall()
            return {'triggers': [dict(r) for r in rows]}

        @app.post('/api/projects/{project_id}/triggers',
                  dependencies=[Depends(_verify_token)])
        def create_project_trigger(project_id: int, body: dict):
            from automatica_db import init_project_db
            import json as _json
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            pdir = row['directory']
            pconn = init_project_db(Path(pdir))
            cfg = body.get('config_json', body.get('config', {}))
            if not isinstance(cfg, str):
                cfg = _json.dumps(cfg or {})
            cur = pconn.execute(
                "INSERT INTO triggers (name, type, config_json, enabled) "
                "VALUES (?, ?, ?, ?)",
                (body.get('name', ''), body.get('type', ''), cfg,
                 1 if body.get('enabled', True) else 0),
            )
            pconn.commit()
            return {'success': True, 'id': cur.lastrowid}

        @app.put('/api/projects/{project_id}/triggers/{trigger_id}',
                 dependencies=[Depends(_verify_token)])
        def update_project_trigger(project_id: int, trigger_id: int, body: dict):
            from automatica_db import init_project_db
            import json as _json
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            pdir = row['directory']
            pconn = init_project_db(Path(pdir))
            allowed = {'name', 'type', 'config_json', 'enabled'}
            sets, vals = [], []
            for k, v in body.items():
                if k not in allowed:
                    continue
                if k == 'config_json' and not isinstance(v, str):
                    v = _json.dumps(v or {})
                if k == 'enabled':
                    v = 1 if v else 0
                sets.append(f'{k} = ?')
                vals.append(v)
            if not sets:
                return {'success': True, 'updated': 0}
            sets.append("updated_at = CURRENT_TIMESTAMP")
            vals.append(trigger_id)
            cur = pconn.execute(
                f"UPDATE triggers SET {', '.join(sets)} WHERE id = ?", vals
            )
            pconn.commit()
            return {'success': True, 'updated': cur.rowcount}

        @app.delete('/api/projects/{project_id}/triggers/{trigger_id}',
                    dependencies=[Depends(_verify_token)])
        def delete_project_trigger(project_id: int, trigger_id: int):
            from automatica_db import init_project_db
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            pdir = row['directory']
            pconn = init_project_db(Path(pdir))
            cur = pconn.execute('DELETE FROM triggers WHERE id = ?', (trigger_id,))
            pconn.commit()
            return {'success': True, 'deleted': cur.rowcount}

        @app.put('/api/projects/{project_id}/pipeline_config',
                 dependencies=[Depends(_verify_token)])
        def set_project_pipeline_config(project_id: int, body: dict):
            from automatica_db import init_project_db, set_project_setting_json
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            pdir = row['directory']
            pconn = init_project_db(Path(pdir))
            set_project_setting_json(pconn, 'pipeline_config', body)
            pconn.commit()
            return {'success': True}

        # ── Configs CRUD (global) + per-project overrides ─────────────────────
        @app.get('/api/configs')
        def list_configs(project_id: int | None = None):
            from automatica_db import get_configs as _get_configs
            conn = _get_global_conn()
            return {'configs': _get_configs(conn)}

        @app.post('/api/configs', dependencies=[Depends(_verify_token)])
        def create_config_route(body: dict):
            from automatica_db import create_config as _create_config
            conn = _get_global_conn()
            cid = _create_config(
                conn,
                name=body.get('name', ''),
                description=body.get('description', ''),
                parent_config_id=body.get('parent_config_id'),
            )
            return {'success': True, 'id': cid}

        @app.put('/api/configs/{config_id}', dependencies=[Depends(_verify_token)])
        def update_config_route(config_id: int, body: dict):
            conn = _get_global_conn()
            allowed = {'name', 'description', 'parent_config_id'}
            sets, vals = [], []
            for k, v in body.items():
                if k in allowed:
                    sets.append(f'{k} = ?'); vals.append(v)
            if not sets:
                return {'success': True, 'updated': 0}
            vals.append(config_id)
            cur = conn.execute(
                f"UPDATE configs SET {', '.join(sets)} WHERE id = ?", vals
            )
            conn.commit()
            return {'success': True, 'updated': cur.rowcount}

        @app.patch('/api/configs/{config_id}',
                   dependencies=[Depends(_verify_token)])
        def patch_config_route(config_id: int, body: dict):
            """Update config name/description and optionally replace overrides.

            Body: {name?, description?, overrides?: {scope: {key: value}}}
            """
            from automatica_db import set_config_override as _set_override
            conn = _get_global_conn()
            allowed = {'name', 'description', 'parent_config_id'}
            sets, vals = [], []
            for k, v in body.items():
                if k in allowed:
                    sets.append(f'{k} = ?'); vals.append(v)
            if sets:
                vals.append(config_id)
                conn.execute(
                    f"UPDATE configs SET {', '.join(sets)} WHERE id = ?", vals
                )
            overrides = body.get('overrides')
            if isinstance(overrides, dict):
                conn.execute(
                    'DELETE FROM config_overrides WHERE config_id = ?',
                    (config_id,),
                )
                for scope, kv in overrides.items():
                    if not isinstance(kv, dict):
                        continue
                    for key, value in kv.items():
                        _set_override(conn, config_id, key,
                                      str(value), scope=scope)
            conn.commit()
            return {'success': True}

        @app.delete('/api/configs/{config_id}',
                    dependencies=[Depends(_verify_token)])
        def delete_config_route(config_id: int):
            conn = _get_global_conn()
            conn.execute('DELETE FROM config_overrides WHERE config_id = ?',
                         (config_id,))
            cur = conn.execute('DELETE FROM configs WHERE id = ?', (config_id,))
            conn.commit()
            return {'success': True, 'deleted': cur.rowcount}

        @app.get('/api/projects/{project_id}/config-overrides')
        def get_project_config_overrides(project_id: int):
            from automatica_db import get_config_overrides as _get_overrides
            conn = _get_global_conn()
            row = conn.execute(
                'SELECT config_id FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row or row['config_id'] is None:
                return {'overrides': {}}
            return {'overrides': _get_overrides(conn, row['config_id'])}

        @app.put('/api/projects/{project_id}/config-overrides/{key}',
                 dependencies=[Depends(_verify_token)])
        def set_project_config_override(project_id: int, key: str, body: dict):
            from automatica_db import (
                set_config_override as _set_override,
                delete_config_override as _del_override,
            )
            conn = _get_global_conn()
            row = conn.execute(
                'SELECT config_id FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row or row['config_id'] is None:
                raise HTTPException(404, 'project has no config attached')
            cid = row['config_id']
            value = body.get('value')
            if value is None:
                _del_override(conn, cid, key)
            else:
                _set_override(conn, cid, key, value, body.get('scope', 'project'))
            conn.commit()
            return {'success': True}

        # ── Standards: DELETE ─────────────────────────────────────────────────
        @app.delete('/api/standards/{filename}',
                    dependencies=[Depends(_verify_token)])
        def delete_standard_route(filename: str):
            import re as _re
            safe = _re.sub(r'[^A-Za-z0-9._-]', '', filename)
            if not safe:
                raise HTTPException(400, 'invalid filename')
            sdir = Path.home() / '.automatica' / 'standards'
            target = sdir / safe
            if not target.exists():
                raise HTTPException(404, 'standard not found')
            target.unlink()
            return {'success': True}

        # ── Pipelines CRUD (file-backed: ~/.automatica/pipelines.json) ────────
        _PIPELINES_FILE = Path.home() / '.automatica' / 'pipelines.json'

        def _load_pipelines_file() -> list:
            import json as _j
            if _PIPELINES_FILE.exists():
                try:
                    data = _j.loads(_PIPELINES_FILE.read_text(encoding='utf-8'))
                    return data if isinstance(data, list) else []
                except Exception:
                    return []
            return []

        def _save_pipelines_file(pipelines: list) -> None:
            import json as _j
            _PIPELINES_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PIPELINES_FILE.write_text(
                _j.dumps(pipelines, indent=2), encoding='utf-8',
            )

        @app.get('/api/pipelines')
        def list_pipelines():
            return {'pipelines': _load_pipelines_file()}

        @app.post('/api/pipelines', dependencies=[Depends(_verify_token)])
        def create_pipeline_route(body: dict):
            pipelines = _load_pipelines_file()
            key = body.get('key', '').strip()
            if not key:
                raise HTTPException(400, 'key required')
            if any(p.get('key') == key for p in pipelines):
                raise HTTPException(409, f'pipeline {key!r} exists')
            pipeline = {
                'key': key,
                'name': body.get('name', key),
                'icon': body.get('icon', 'pipe'),
                'owner': body.get('owner', 'global'),
                'stages': body.get('stages', []),
            }
            pipelines.append(pipeline)
            _save_pipelines_file(pipelines)
            return {'success': True, 'pipeline': pipeline}

        @app.get('/api/pipelines/{key}')
        def get_pipeline_route(key: str):
            for p in _load_pipelines_file():
                if p.get('key') == key:
                    return p
            raise HTTPException(404, 'pipeline not found')

        @app.put('/api/pipelines/{key}',
                 dependencies=[Depends(_verify_token)])
        def update_pipeline_route(key: str, body: dict):
            pipelines = _load_pipelines_file()
            for i, p in enumerate(pipelines):
                if p.get('key') == key:
                    for f in ('name', 'icon', 'owner', 'stages'):
                        if f in body:
                            p[f] = body[f]
                    pipelines[i] = p
                    _save_pipelines_file(pipelines)
                    return {'success': True, 'pipeline': p}
            raise HTTPException(404, 'pipeline not found')

        @app.put('/api/pipelines',
                 dependencies=[Depends(_verify_token)])
        def replace_pipelines_route(body: dict):
            """Replace the entire pipelines list (used for bulk save)."""
            pipelines = body.get('pipelines', body if isinstance(body, list) else [])
            if not isinstance(pipelines, list):
                raise HTTPException(400, 'expected list of pipelines')
            _save_pipelines_file(pipelines)
            return {'success': True, 'count': len(pipelines)}

        @app.delete('/api/pipelines/{key}',
                    dependencies=[Depends(_verify_token)])
        def delete_pipeline_route(key: str):
            pipelines = _load_pipelines_file()
            new_list = [p for p in pipelines if p.get('key') != key]
            if len(new_list) == len(pipelines):
                raise HTTPException(404, 'pipeline not found')
            _save_pipelines_file(new_list)
            return {'success': True}

        # ── Global triggers (file-backed: ~/.automatica/triggers.json) ────────
        _TRIGGERS_FILE = Path.home() / '.automatica' / 'triggers.json'

        def _load_triggers_file(path: Path) -> list:
            import json as _j
            if path.exists():
                try:
                    data = _j.loads(path.read_text(encoding='utf-8'))
                    return data if isinstance(data, list) else []
                except Exception:
                    return []
            return []

        def _save_triggers_file(path: Path, triggers: list) -> None:
            import json as _j
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_j.dumps(triggers, indent=2), encoding='utf-8')

        @app.get('/api/triggers')
        def list_global_triggers():
            return {'triggers': _load_triggers_file(_TRIGGERS_FILE)}

        @app.post('/api/triggers',
                  dependencies=[Depends(_verify_token)])
        def save_global_triggers(body: dict):
            triggers = body.get('triggers', body if isinstance(body, list) else [])
            if not isinstance(triggers, list):
                raise HTTPException(400, 'expected list of triggers')
            _save_triggers_file(_TRIGGERS_FILE, triggers)
            return {'success': True, 'count': len(triggers)}

        @app.put('/api/projects/{project_id}/triggers',
                 dependencies=[Depends(_verify_token)])
        def replace_project_triggers(project_id: int, body: dict):
            """Replace the project's triggers with the supplied list."""
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?', (project_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            triggers = body.get('triggers', body if isinstance(body, list) else [])
            if not isinstance(triggers, list):
                raise HTTPException(400, 'expected list of triggers')
            tpath = Path(row['directory']) / 'automatica' / 'triggers.json'
            _save_triggers_file(tpath, triggers)
            return {'success': True, 'count': len(triggers)}

        # ── Managers CRUD ─────────────────────────────────────────────────────
        @app.get('/api/managers')
        def list_managers():
            from automatica_db import get_managers as _gm, get_manager_projects as _gmp
            conn = _get_global_conn()
            managers = _gm(conn)
            for m in managers:
                m['projects'] = _gmp(conn, m['id'])
            return {'managers': managers}

        @app.post('/api/managers', dependencies=[Depends(_verify_token)])
        def create_manager_route(body: dict):
            from automatica_db import (
                create_manager as _cm,
                add_manager_project as _amp,
            )
            conn = _get_global_conn()
            mid = _cm(
                conn,
                name=body.get('name', ''),
                goal=body.get('goal', ''),
                config_id=body.get('config_id'),
            )
            for pdir in body.get('project_dirs', []) or []:
                try:
                    _amp(conn, mid, pdir)
                except Exception:
                    pass
            conn.commit()
            return {'success': True, 'id': mid}

        @app.patch('/api/managers/{manager_id}',
                   dependencies=[Depends(_verify_token)])
        def patch_manager_route(manager_id: int, body: dict):
            from automatica_db import (
                update_manager as _um,
                add_manager_project as _amp,
                remove_manager_project as _rmp,
                get_manager_projects as _gmp,
            )
            conn = _get_global_conn()
            fields = {k: v for k, v in body.items()
                      if k in {'name', 'goal', 'config_id'}}
            if fields:
                _um(conn, manager_id, **fields)
            if 'project_dirs' in body and isinstance(body['project_dirs'], list):
                current = {p['directory'] for p in _gmp(conn, manager_id)}
                want = set(body['project_dirs'])
                for pdir in want - current:
                    try: _amp(conn, manager_id, pdir)
                    except Exception: pass
                for pdir in current - want:
                    try: _rmp(conn, manager_id, pdir)
                    except Exception: pass
            conn.commit()
            return {'success': True}

        @app.delete('/api/managers/{manager_id}',
                    dependencies=[Depends(_verify_token)])
        def delete_manager_route(manager_id: int):
            conn = _get_global_conn()
            conn.execute('DELETE FROM manager_projects WHERE manager_id = ?',
                         (manager_id,))
            cur = conn.execute('DELETE FROM managers WHERE id = ?', (manager_id,))
            conn.commit()
            return {'success': True, 'deleted': cur.rowcount}

        # ── VCS operations (subprocess git wrappers) ──────────────────────────
        def _resolve_project_dir(project_id: int) -> str:
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?',
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            return row['directory']

        def _git(args: list, cwd: str, timeout: int = 30) -> dict:
            import subprocess as _sp
            try:
                r = _sp.run(['git'] + args, cwd=cwd, capture_output=True,
                            text=True, timeout=timeout)
                return {
                    'success': r.returncode == 0,
                    'output': r.stdout,
                    'error': r.stderr,
                    'returncode': r.returncode,
                }
            except _sp.TimeoutExpired:
                return {'success': False, 'output': '', 'error': 'timeout',
                        'returncode': -1}
            except FileNotFoundError:
                return {'success': False, 'output': '', 'error': 'git not found',
                        'returncode': -1}

        @app.get('/api/vcs/status')
        def vcs_status_route(project_id: int):
            cwd = _resolve_project_dir(project_id)
            branch = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd)
            short = _git(['status', '--porcelain'], cwd)
            ahead_behind = _git(
                ['rev-list', '--left-right', '--count', '@{upstream}...HEAD'],
                cwd,
            )
            ahead, behind = 0, 0
            if ahead_behind['success']:
                parts = ahead_behind['output'].strip().split()
                if len(parts) == 2:
                    behind, ahead = int(parts[0]), int(parts[1])
            dirty = []
            if short['success']:
                for ln in short['output'].splitlines():
                    ln = ln.strip()
                    if ln:
                        dirty.append(ln[3:] if len(ln) > 3 else ln)
            return {
                'branch': branch['output'].strip() if branch['success'] else '',
                'dirty_files': dirty,
                'ahead': ahead,
                'behind': behind,
            }

        @app.post('/api/vcs/commit',
                  dependencies=[Depends(_verify_token)])
        def vcs_commit_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            files = body.get('files') or []
            msg = body.get('message', '').strip()
            if not msg:
                raise HTTPException(400, 'message required')
            if files:
                _git(['add'] + list(files), cwd)
            else:
                _git(['add', '-A'], cwd)
            return _git(['commit', '-m', msg], cwd)

        @app.post('/api/vcs/push',
                  dependencies=[Depends(_verify_token)])
        def vcs_push_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            args = ['push', body.get('remote', 'origin')]
            if body.get('branch'):
                args.append(body['branch'])
            return _git(args, cwd, timeout=120)

        @app.post('/api/vcs/pull',
                  dependencies=[Depends(_verify_token)])
        def vcs_pull_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            return _git(['pull', body.get('remote', 'origin')], cwd, timeout=120)

        @app.post('/api/vcs/branch',
                  dependencies=[Depends(_verify_token)])
        def vcs_branch_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            name = body.get('branch_name', '').strip()
            if not name:
                raise HTTPException(400, 'branch_name required')
            return _git(['checkout', '-B', name], cwd)

        @app.post('/api/vcs/reset',
                  dependencies=[Depends(_verify_token)])
        def vcs_reset_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            mode = body.get('mode', 'soft')
            if mode not in ('soft', 'hard', 'mixed'):
                raise HTTPException(400, 'mode must be soft|hard|mixed')
            ref = body.get('ref', 'HEAD~1')
            return _git(['reset', f'--{mode}', ref], cwd)

        @app.post('/api/vcs/pr',
                  dependencies=[Depends(_verify_token)])
        def vcs_pr_route(body: dict):
            cwd = _resolve_project_dir(body.get('project_id', 0))
            title = body.get('title', '').strip()
            if not title:
                raise HTTPException(400, 'title required')
            args = ['pr', 'create', '--title', title,
                    '--body', body.get('body', '') or title,
                    '--base', body.get('base_branch', 'main')]
            import subprocess as _sp
            try:
                r = _sp.run(['gh'] + args, cwd=cwd, capture_output=True,
                            text=True, timeout=60)
                return {
                    'success': r.returncode == 0,
                    'output': r.stdout,
                    'error': r.stderr,
                    'url': r.stdout.strip().splitlines()[-1] if r.stdout else '',
                }
            except FileNotFoundError:
                return {'success': False, 'error': 'gh CLI not installed'}
            except _sp.TimeoutExpired:
                return {'success': False, 'error': 'timeout'}

        @app.get('/api/vcs/diff')
        def vcs_diff_route(project_id: int):
            cwd = _resolve_project_dir(project_id)
            r = _git(['diff', '--no-color'], cwd)
            return {'success': r['success'], 'diff': r['output']}

        # ── Action log (project-scoped) ───────────────────────────────────────
        def _open_pconn(project_id: int):
            from automatica_db import init_project_db
            row = _get_global_conn().execute(
                'SELECT directory FROM known_projects WHERE id = ?',
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f'project {project_id} not known')
            return init_project_db(Path(row['directory']))

        @app.get('/api/projects/{project_id}/action-log')
        def get_action_log_route(project_id: int, scope: str = '',
                                 scope_id: str = '', limit: int = 200):
            pconn = _open_pconn(project_id)
            try:
                if scope and scope_id:
                    rows = pconn.execute(
                        'SELECT actor, action, detail, scope, scope_id, '
                        'created_at AS timestamp '
                        'FROM action_log WHERE scope = ? AND scope_id = ? '
                        'ORDER BY created_at ASC LIMIT ?',
                        (scope, scope_id, limit),
                    ).fetchall()
                else:
                    rows = pconn.execute(
                        'SELECT actor, action, detail, scope, scope_id, '
                        'created_at AS timestamp '
                        'FROM action_log ORDER BY created_at DESC LIMIT ?',
                        (limit,),
                    ).fetchall()
                return {'logs': [dict(r) for r in rows]}
            except Exception as exc:
                return {'logs': [], 'error': str(exc)}

        # ── VCS commits (project-scoped, from project DB) ─────────────────────
        @app.get('/api/projects/{project_id}/vcs-commits')
        def get_entity_commits_route(project_id: int,
                                     entity_type: str = '',
                                     entity_id: str = ''):
            pconn = _open_pconn(project_id)
            try:
                if entity_type and entity_id:
                    rows = pconn.execute(
                        'SELECT commit_hash, message, branch, committed_at '
                        'FROM vcs_commits WHERE entity_type = ? AND entity_id = ? '
                        'ORDER BY committed_at DESC',
                        (entity_type, str(entity_id)),
                    ).fetchall()
                else:
                    rows = pconn.execute(
                        'SELECT commit_hash, message, branch, committed_at, '
                        'entity_type, entity_id '
                        'FROM vcs_commits ORDER BY committed_at DESC LIMIT 100',
                    ).fetchall()
                return {'commits': [dict(r) for r in rows]}
            except Exception as exc:
                return {'commits': [], 'error': str(exc)}

        @app.post('/api/projects/{project_id}/vcs-commits/branches',
                  dependencies=[Depends(_verify_token)])
        def get_entity_branches_route(project_id: int, body: dict):
            pconn = _open_pconn(project_id)
            entity_type = body.get('entity_type', '')
            entity_ids = body.get('entity_ids', []) or []
            if not entity_type or not entity_ids:
                return {'branches': {}}
            try:
                placeholders = ','.join('?' * len(entity_ids))
                rows = pconn.execute(
                    f'SELECT entity_id, branch FROM vcs_commits '
                    f'WHERE entity_type = ? AND entity_id IN ({placeholders}) '
                    f'GROUP BY entity_id',
                    [entity_type] + [str(e) for e in entity_ids],
                ).fetchall()
                return {'branches': {r['entity_id']: r['branch'] for r in rows}}
            except Exception as exc:
                return {'branches': {}, 'error': str(exc)}

        # ── Subtasks ──────────────────────────────────────────────────────────
        @app.get('/api/tasks/{task_display_id}/subtasks')
        def get_subtasks_route(task_display_id: str, project_id: int):
            from automatica_db import get_subtasks as _gs
            pconn = _open_pconn(project_id)
            try:
                return {'subtasks': _gs(pconn, task_display_id)}
            except Exception:
                return {'subtasks': []}

        @app.post('/api/tasks/{task_display_id}/subtasks',
                  dependencies=[Depends(_verify_token)])
        def create_subtask_route(task_display_id: str, body: dict):
            from automatica_db import create_subtask as _cs
            pconn = _open_pconn(body.get('project_id', 0))
            try:
                result = _cs(pconn, task_display_id,
                             body.get('title', ''),
                             body.get('description', ''))
                pconn.commit()
                return {'success': True, 'subtask': result}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        @app.post('/api/subtasks/{subtask_display_id}/complete',
                  dependencies=[Depends(_verify_token)])
        def complete_subtask_route(subtask_display_id: str, body: dict):
            from automatica_db import complete_subtask as _comp
            pconn = _open_pconn(body.get('project_id', 0))
            try:
                _comp(pconn, subtask_display_id, body.get('result', ''))
                pconn.commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Retrospectives ────────────────────────────────────────────────────
        @app.get('/api/sprints/{sprint_id}/retrospective')
        def get_retrospective_route(sprint_id: int, project_id: int):
            from automatica_db import get_retrospective as _gr
            pconn = _open_pconn(project_id)
            try:
                return {'retro': _gr(pconn, sprint_id) or {}}
            except Exception:
                return {'retro': {}}

        @app.post('/api/sprints/{sprint_id}/retrospective',
                  dependencies=[Depends(_verify_token)])
        def save_retrospective_route(sprint_id: int, body: dict):
            from automatica_db import save_retrospective as _sr
            pconn = _open_pconn(body.get('project_id', 0))
            try:
                _sr(pconn, sprint_id, body.get('content', ''))
                pconn.commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Comments (task-level) ─────────────────────────────────────────────
        @app.get('/api/tasks/{task_display_id}/comments')
        def get_comments_route(task_display_id: str, project_id: int):
            from automatica_db import get_comments as _gc
            pconn = _open_pconn(project_id)
            try:
                return {'comments': _gc(pconn, task_display_id)}
            except Exception:
                return {'comments': []}

        @app.post('/api/tasks/{task_display_id}/comments',
                  dependencies=[Depends(_verify_token)])
        def add_comment_route(task_display_id: str, body: dict):
            from automatica_db import add_comment as _ac
            pconn = _open_pconn(body.get('project_id', 0))
            try:
                result = _ac(pconn, task_display_id,
                             body.get('author', 'user'),
                             body.get('content', ''))
                pconn.commit()
                return {'success': True, 'comment': result}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Agent stats (project-scoped) ──────────────────────────────────────
        @app.get('/api/projects/{project_id}/agent-stats')
        def get_all_agent_stats_route(project_id: int):
            from automatica_db import get_all_agent_stats as _gas
            pconn = _open_pconn(project_id)
            try:
                return {'stats': _gas(pconn)}
            except Exception:
                return {'stats': []}

        @app.get('/api/projects/{project_id}/agents/{slug}/stats')
        def get_agent_stats_route(project_id: int, slug: str):
            from automatica_db import get_agent_stats as _gas1
            pconn = _open_pconn(project_id)
            try:
                return {'stats': _gas1(pconn, slug)}
            except Exception:
                return {'stats': {}}

        # ── Sprint cost summary ───────────────────────────────────────────────
        @app.get('/api/sprints/{sprint_id}/cost-summary')
        def get_sprint_cost_route(sprint_id: int, project_id: int):
            from automatica_db import get_sprint_cost_summary as _gsc
            pconn = _open_pconn(project_id)
            try:
                return _gsc(pconn, sprint_id) or {}
            except Exception:
                return {}

        # ── Complexity routing (global) ───────────────────────────────────────
        @app.get('/api/complexity-routing')
        def get_complexity_routing_route(project_name: str = '*'):
            from automatica_db import get_complexity_routing as _gcr
            try:
                return {'routing': _gcr(_get_global_conn(), project_name)}
            except Exception:
                return {'routing': []}

        @app.post('/api/complexity-routing',
                  dependencies=[Depends(_verify_token)])
        def upsert_complexity_routing_route(body: dict):
            from automatica_db import setup_complexity_routing_by_name as _scr
            try:
                _scr(_get_global_conn(),
                     project_name=body.get('project_name', '*'),
                     min_complexity=body.get('min_complexity', 1),
                     max_complexity=body.get('max_complexity', 10),
                     provider_id=body.get('provider_id'))
                _get_global_conn().commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Project touch (update last_opened) ────────────────────────────────
        @app.post('/api/projects/touch',
                  dependencies=[Depends(_verify_token)])
        def touch_project_route(body: dict):
            from automatica_db import update_project_last_opened as _upd
            try:
                _upd(_get_global_conn(), body.get('directory', ''))
                _get_global_conn().commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Project create (DB-only, file system optional) ────────────────────
        @app.post('/api/projects',
                  dependencies=[Depends(_verify_token)])
        def create_project_route(body: dict):
            from automatica_db import register_project as _rp
            try:
                pid = _rp(
                    _get_global_conn(),
                    name=body.get('name', ''),
                    directory=body.get('directory', body.get('path', '')),
                    goal=body.get('goal', ''),
                )
                _get_global_conn().commit()
                return {'success': True, 'id': pid}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        # ── Generic per-project settings ──────────────────────────────────────
        @app.get('/api/projects/{project_id}/settings/{key}')
        def get_project_setting_route(project_id: int, key: str):
            from automatica_db import get_project_setting as _gps
            pconn = _open_pconn(project_id)
            try:
                value = _gps(pconn, key, default='')
                return {'key': key, 'value': value}
            except Exception as exc:
                return {'key': key, 'value': '', 'error': str(exc)}

        @app.put('/api/projects/{project_id}/settings/{key}',
                 dependencies=[Depends(_verify_token)])
        def set_project_setting_route(project_id: int, key: str, body: dict):
            from automatica_db import set_project_setting as _sps
            pconn = _open_pconn(project_id)
            try:
                _sps(pconn, key, str(body.get('value', '')))
                pconn.commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        @app.delete('/api/projects/{project_id}',
                    dependencies=[Depends(_verify_token)])
        def delete_project_route(project_id: int):
            try:
                conn = _get_global_conn()
                conn.execute('DELETE FROM known_projects WHERE id = ?',
                             (project_id,))
                conn.commit()
                return {'success': True}
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        return app
