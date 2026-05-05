"""Provider auto-discovery: scan PATH and known local endpoints for AI providers.

Read-only by design. Returns suggested provider rows that the caller can
choose to insert via the existing authenticated POST /api/providers endpoint.

Two scan paths:
  * subprocess CLIs — `shutil.which()` looks for known binaries on PATH.
  * HTTP providers — issues a short-timeout GET to standard local endpoints
    (e.g. Ollama on localhost:11434) and reports any that respond.

Security: this module does NOT execute any binary. It only reports paths
discovered on the user's PATH. Persisting discovered rows to the providers
table is a separate, authenticated step (POST /api/providers).
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Tuple

# (display_name, executable, args, model_hint)
# Args use safe permission modes — never `--dangerously-skip-permissions`.
_KNOWN_SUBPROCESS_PROVIDERS: List[Tuple[str, str, List[str], str]] = [
    ("Claude-Opus",
     "claude",
     ["--permission-mode", "acceptEdits", "--model", "claude-opus-4-7", "-p"],
     "claude-opus-4-7"),
    ("Claude-Sonnet",
     "claude",
     ["--permission-mode", "acceptEdits", "--model", "claude-sonnet-4-6", "-p"],
     "claude-sonnet-4-6"),
    ("Claude-Haiku",
     "claude",
     ["--permission-mode", "acceptEdits", "--model", "claude-haiku-4-5", "-p"],
     "claude-haiku-4-5"),
    ("Codex",
     "codex",
     ["--quiet"],
     ""),
    ("Gemini-CLI",
     "gemini",
     ["--prompt"],
     ""),
]

# (display_name, base_url, ping_path, default_model, provider_type)
_KNOWN_HTTP_PROVIDERS: List[Tuple[str, str, str, str, str]] = [
    ("Ollama-localhost",
     "http://localhost:11434",
     "/api/tags",
     "llama3",
     "http_local_chat"),
]


def discover_subprocess_providers() -> List[dict]:
    """Return rows suggested for any known CLI binaries found on PATH."""
    out: List[dict] = []
    for name, executable, args, model in _KNOWN_SUBPROCESS_PROVIDERS:
        resolved = shutil.which(executable)
        if not resolved:
            continue
        out.append({
            "name": name,
            "provider_type": "subprocess_cli",
            "model_name": model,
            "enabled": 1,
            "max_tokens": 4096,
            "config": {
                "executable": resolved,
                "args": list(args),
                "timeout": 600,
                "mode": "agentic",
            },
            "discovered_path": resolved,
        })
    return out


def discover_http_providers(timeout_s: float = 2.0) -> List[dict]:
    """Probe known local HTTP endpoints; report ones that answer in `timeout_s`."""
    out: List[dict] = []
    for name, base_url, ping_path, model, ptype in _KNOWN_HTTP_PROVIDERS:
        url = base_url.rstrip("/") + ping_path
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if 200 <= resp.status < 300:
                    out.append({
                        "name": name,
                        "provider_type": ptype,
                        "model_name": model,
                        "enabled": 1,
                        "max_tokens": 4096,
                        "config": {
                            "url": base_url,
                            "model": model,
                        },
                        "discovered_url": base_url,
                    })
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return out


def discover_all() -> List[dict]:
    """Return all discovered providers (subprocess + HTTP)."""
    return discover_subprocess_providers() + discover_http_providers()


__all__ = [
    "discover_subprocess_providers",
    "discover_http_providers",
    "discover_all",
]
