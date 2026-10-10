"""Forward model API requests to a backend: apply route defaults, stream the reply, read timings."""

from __future__ import annotations

import http.client
import json
from typing import Any, Callable
from urllib.parse import urlsplit

from local_ai.gateway_config import BackendSpec, RouteSpec

# Headers passed through to the backend; everything else (notably the gateway's own
# Authorization / x-api-key) is dropped.
_FORWARD_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta")
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}


def prepare_body(body: dict[str, Any], route: RouteSpec | None, backend: BackendSpec) -> dict[str, Any]:
    """The request as the backend should see it: its own model name plus the route's defaults."""
    prepared = dict(body)
    prepared["model"] = backend.model
    if route is None:
        return prepared
    for key, value in route.params.items():
        prepared.setdefault(key, value)
    if route.max_tokens:
        for key in ("max_tokens", "max_completion_tokens"):
            if key in prepared:
                prepared[key] = min(int(prepared[key]), route.max_tokens)
        if "max_tokens" not in prepared and "max_completion_tokens" not in prepared:
            prepared["max_tokens"] = route.max_tokens
    if route.thinking is not None:
        kwargs = dict(prepared.get("chat_template_kwargs") or {})
        kwargs["enable_thinking"] = route.thinking
        prepared["chat_template_kwargs"] = kwargs
        if not route.thinking:
            prepared.pop("thinking", None)  # Anthropic-style request for extended thinking
    return prepared


def _timings_from_sse(buffer: str) -> dict | None:
    found = None
    for line in buffer.splitlines():
        if line.startswith("data:") and '"timings"' in line:
            try:
                found = json.loads(line[5:].strip()).get("timings") or found
            except json.JSONDecodeError:
                pass
    return found


def forward(
    backend: BackendSpec,
    method: str,
    path: str,
    body: bytes | None,
    headers: dict[str, str],
    send_head: Callable[[int, list[tuple[str, str]]], None],
    send_chunk: Callable[[bytes], None],
    timeout_s: float = 3600.0,
) -> dict | None:
    """Relay one request; returns llama-server's `timings` when the reply carries them."""
    target = urlsplit(backend.url)
    conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(target.hostname, target.port, timeout=timeout_s)
    out_headers = {k: v for k, v in headers.items() if k.lower() in _FORWARD_HEADERS}
    if backend.api_key:
        out_headers["Authorization"] = f"Bearer {backend.api_key}"
    try:
        conn.request(method, (target.path or "") + path, body=body, headers=out_headers)
        reply = conn.getresponse()
        reply_headers = [(k, v) for k, v in reply.getheaders() if k.lower() not in _HOP_BY_HOP]
        send_head(reply.status, reply_headers)
        streaming = "text/event-stream" in (reply.getheader("Content-Type") or "")
        if streaming:
            tail = ""
            while chunk := reply.read1(65536):
                send_chunk(chunk)
                tail = (tail + chunk.decode("utf-8", errors="replace"))[-16384:]
            return _timings_from_sse(tail)
        payload = reply.read()
        send_chunk(payload)
        try:
            return json.loads(payload).get("timings")
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            return None
    finally:
        conn.close()


def call_json(backend: BackendSpec, path: str, payload: dict, timeout_s: float = 3600.0) -> dict:
    """A non-streaming request made by the gateway itself (planning, health checks)."""
    chunks: list[bytes] = []
    status: list[int] = []
    forward(backend, "POST", path, json.dumps(payload).encode(),
            {"content-type": "application/json"},
            lambda code, _h: status.append(code), chunks.append, timeout_s)
    data = json.loads(b"".join(chunks) or b"{}")
    if status and status[0] >= 400:
        raise RuntimeError(f"{backend.name} {path} -> {status[0]}: {json.dumps(data)[:300]}")
    return data
