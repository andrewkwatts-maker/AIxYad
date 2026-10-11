"""The local_ai gateway: one authenticated HTTP endpoint in front of every model server.

    python -m local_ai.gateway                 # serve on the addresses in local_ai.yaml
    python -m local_ai.gateway --print-token   # show the token (for the phone or the extension)

See local_ai/API.md for the endpoints. Model requests are routed by their `model` field,
queued per backend by the scheduler, and streamed straight through.
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aixyad_paths
from local_ai.gateway_config import BackendSpec, GatewaySpec, _parallel_slots, load_gateway
from local_ai.jobs import JobManager
from local_ai.llama_server import LlamaServer
from local_ai.profiles import ServerProfile
from local_ai.proxy import forward, prepare_body
from local_ai.scheduler import BackendUnavailable, Scheduler

WEB_PAGE = Path(__file__).with_name("web") / "index.html"
FAVICON_PAGE = Path(__file__).with_name("web") / "favicon.svg"
_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"]+")
COOKIE = "aixyad_token"
MAX_BODY = 32 * 2**20


def ensure_token(path: Path) -> str:
    """The gateway's bearer token, created on first use."""
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    return token


class ServerController:
    """Starts and stops managed backends as llama-server processes (scheduler.Controller)."""

    def __init__(self, profiles: dict[str, ServerProfile], log_dir: Path) -> None:
        self._profiles = profiles
        self._log_dir = log_dir
        self._servers: dict[str, LlamaServer] = {}
        self._lock = threading.Lock()

    def start(self, backend: BackendSpec, profile: str, variables: dict[str, str]) -> None:
        name = profile or backend.profile or ""
        if name not in self._profiles:
            raise ValueError(f"unknown profile {name!r}")
        server = LlamaServer(self._profiles[name].with_vars(variables), self._log_dir)
        server.start()
        with self._lock:
            self._servers[backend.name] = server

    def stop(self, backend: BackendSpec) -> None:
        with self._lock:
            server = self._servers.pop(backend.name, None)
        if server:
            server.stop()

    def stop_all(self) -> None:
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for server in servers:
            server.stop()


class Gateway:
    def __init__(self, spec: GatewaySpec, controller: Any = None, jobs: JobManager | None = None) -> None:
        self.spec = spec
        self.token = ensure_token(spec.token_file)
        self.controller = controller or ServerController(spec.profiles, aixyad_paths.home() / "servers")
        self.scheduler = Scheduler(list(spec.backends.values()), self.controller,
                                   max_hold_s=spec.max_hold_s, idle_unload_s=spec.idle_unload_s)
        host, port = spec.listen[0]
        self.url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
        self.jobs = jobs or JobManager(spec.jobs, self.url, self.token)
        self._stop = threading.Event()

    def authorized(self, headers: Any, query: dict[str, list[str]] | None = None) -> bool:
        candidates = []
        auth = headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        if headers.get("x-api-key"):
            candidates.append(headers["x-api-key"].strip())
        for part in headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE:
                candidates.append(value)
        return any(hmac.compare_digest(c.encode(), self.token.encode()) for c in candidates if c)

    def status(self) -> dict:
        return {"version": 1, "backends": self.scheduler.status(),
                "routes": {name: r.backend for name, r in self.spec.routes.items()},
                "jobs": self.jobs.counts()}

    def models(self) -> dict:
        names = list(self.spec.routes) + [b.model for b in self.spec.backends.values()]
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "local"} for n in names]}

    def background(self) -> None:
        """Health of LAN backends every 15 s; scheduler housekeeping every 5 s."""
        last_health = 0.0
        while not self._stop.wait(5):
            self.scheduler.tick()
            if time.monotonic() - last_health < 15:
                continue
            last_health = time.monotonic()
            for backend in self.spec.backends.values():
                if backend.managed:
                    continue
                request = urllib.request.Request(backend.url + backend.health_path)
                if backend.api_key:
                    request.add_header("Authorization", f"Bearer {backend.api_key}")
                try:
                    with urllib.request.urlopen(request, timeout=5) as reply:
                        self.scheduler.set_reachable(backend.name, reply.status == 200)
                except (urllib.error.URLError, OSError) as exc:
                    self.scheduler.set_reachable(backend.name, False, str(exc)[:200])

    def shutdown(self) -> None:
        self._stop.set()
        self.controller.stop_all()


class Handler(BaseHTTPRequestHandler):
    server_version = "AIxYadGateway/1"
    gateway: Gateway  # set on the subclass made by serve()

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # one terse line per request, token redacted
        line = _TOKEN_IN_URL.sub(r"\1<redacted>", fmt % args)
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.address_string()} {line}\n")

    def _json(self, code: int, payload: Any, extra: list[tuple[str, str]] = ()) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str, kind: str = "gateway_error") -> None:
        self._json(code, {"error": {"message": message, "type": kind}})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    # -- dispatch ------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
        gw = self.gateway
        if method == "GET" and path == "/health":
            return self._json(200, {"status": "ok"})
        if method == "GET" and path == "/favicon.svg":
            return self._favicon()
        if method == "GET" and path == "/ui" and "token" in query:
            if not hmac.compare_digest(query["token"][0].encode(), gw.token.encode()):
                return self._error(401, "wrong token")
            self.send_response(303)
            self.send_header("Set-Cookie", f"{COOKIE}={gw.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
            self.send_header("Location", "/ui")
            self.end_headers()
            return None
        if not gw.authorized(self.headers):
            return self._error(401, "missing or wrong token", "authentication_error")
        try:
            if method == "GET" and path == "/ui":
                return self._page()
            if path.startswith("/v1/"):
                return self._model_request(method)
            return self._api(method, path, query)
        except KeyError as exc:
            return self._error(404, f"not found: {exc}")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except RuntimeError as exc:
            return self._error(409, str(exc))
        except BackendUnavailable as exc:
            return self._error(503, str(exc), "overloaded_error")

    def _page(self) -> None:
        data = WEB_PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _favicon(self) -> None:
        data = FAVICON_PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        gw, parts = self.gateway, path.strip("/").split("/")
        if parts[0] != "gateway":
            raise KeyError(path)
        if method == "GET" and parts[1:] == ["status"]:
            return self._json(200, gw.status())
        if method == "GET" and parts[1:] == ["profiles"]:
            return self._json(200, [{"name": p.name, "description": p.description, "vars": p.vars}
                                    for p in gw.spec.profiles.values()])
        if method == "POST" and len(parts) == 4 and parts[1] == "backends" and parts[3] in ("start", "stop"):
            if parts[2] not in gw.spec.backends:
                raise KeyError(parts[2])
            if parts[3] == "start":
                body = self._body()
                name = body.get("profile") or gw.spec.backends[parts[2]].profile
                if name not in gw.spec.profiles:
                    raise ValueError(f"unknown profile {name!r}")
                variables = {str(k): str(v) for k, v in (body.get("vars") or {}).items()}
                profile = gw.spec.profiles[name].with_vars(variables)  # rejects unknown names
                gw.scheduler.start(parts[2], name, variables, _parallel_slots(profile.args))
            else:
                gw.scheduler.stop(parts[2])
            return self._json(202, {"ok": True})
        if parts[1:2] == ["jobs"]:
            if method == "POST" and len(parts) == 2:
                return self._json(201, {"id": gw.jobs.create(self._body())})
            if method == "GET" and len(parts) == 2:
                return self._json(200, gw.jobs.list())
            if method == "GET" and len(parts) == 3:
                return self._json(200, gw.jobs.get(parts[2]))
            if method == "GET" and parts[3:] == ["events"]:
                after = int((query.get("after") or ["0"])[0])
                return self._json(200, gw.jobs.events(parts[2], after))
            if method == "POST" and parts[3:] == ["approve"]:
                gw.jobs.approve(parts[2], self._body().get("plan"))
                return self._json(200, {"ok": True})
            if method == "POST" and parts[3:] == ["cancel"]:
                gw.jobs.cancel(parts[2])
                return self._json(200, {"ok": True})
        raise KeyError(path)

    def _model_request(self, method: str) -> None:
        gw = self.gateway
        if method == "GET" and urlsplit(self.path).path.rstrip("/") == "/v1/models":
            return self._json(200, gw.models())
        payload = self._body()
        route, backend = gw.spec.route_for(payload.get("model"))
        prepared = json.dumps(prepare_body(payload, route, backend)).encode()
        lease = gw.scheduler.acquire(backend.name, gw.spec.queue_timeout_s)
        started = False

        def send_head(code: int, headers: list[tuple[str, str]]) -> None:
            nonlocal started
            started = True
            self.send_response(code)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("X-AIxYad-Backend", backend.name)
            self.end_headers()

        def send_chunk(chunk: bytes) -> None:
            self.wfile.write(chunk)
            self.wfile.flush()

        try:
            timings = forward(backend, method, self.path, prepared, dict(self.headers.items()), send_head, send_chunk)
            lease.report(timings)
        except (ConnectionError, OSError) as exc:
            if not started:
                self._error(502, f"{backend.name} unreachable: {exc}", "api_error")
        finally:
            lease.release()
        return None


def serve(spec: GatewaySpec) -> None:
    gateway = Gateway(spec)
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    servers = [ThreadingHTTPServer(address, handler) for address in spec.listen]
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=gateway.background, daemon=True).start()
    print(f"gateway on {', '.join(f'http://{h}:{p}' for h, p in spec.listen)}; "
          f"phone/browser: {gateway.url}/ui?token=<token> (python -m local_ai.gateway --print-token)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
        gateway.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print-token", action="store_true")
    args = parser.parse_args(argv)
    spec = load_gateway()
    if args.print_token:
        print(ensure_token(spec.token_file))
        return 0
    serve(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
