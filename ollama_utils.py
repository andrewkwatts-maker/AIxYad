#!/usr/bin/env python3
#====== AIxYad/ollama_utils.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
"""
Shared utilities for querying the local Ollama instance.

No UI dependencies — safe to import anywhere.
"""

import json
import urllib.request
import urllib.error
from typing import List

OLLAMA_ENDPOINT_DEFAULT = "http://localhost:11434"

# Known vision-capable model name substrings
_VISION_CAPABLE_SUBSTRINGS = {
    "llava", "llama3.2-vision", "moondream", "bakllava",
    "llava-llama3", "minicpm-v", "cogvlm", "internvl",
}

# Popular models to show even when not installed (for pull/discovery)
OLLAMA_POPULAR_MODELS: List[str] = [
    "llama3.3", "llama3.2", "llama3.1", "llama3",
    "llama3.2-vision", "llama3.2-vision:11b-instruct-q4_K_S",
    "deepseek-coder-v2", "deepseek-coder-v2:16b-lite-instruct-q4_K_M",
    "deepseek-r1",
    "mistral", "mixtral",
    "phi4", "phi3",
    "gemma2", "gemma2:2b",
    "qwen2.5-coder", "qwen2.5",
    "codellama",
    "starcoder2",
    "command-r",
    "moondream",
    "llava",
    "nomic-embed-text",
]


def list_installed_models(
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 5,
) -> List[str]:
    """Query Ollama /api/tags and return a sorted list of installed model names.

    Returns bare names with no :tag suffix (e.g. "llama3.2" not "llama3.2:latest").
    Returns empty list if Ollama is not reachable.
    """
    try:
        url = f"{endpoint}/api/tags"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names: List[str] = []
        for m in data.get("models", []):
            raw = m.get("name", "")
            name = raw.split(":")[0]
            if name and name not in names:
                names.append(name)
        return sorted(names)
    except Exception:
        return []


def list_installed_models_full(
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 5,
) -> List[str]:
    """Like list_installed_models() but preserves the full tag (e.g. 'llama3.2:latest')."""
    try:
        url = f"{endpoint}/api/tags"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names: List[str] = []
        for m in data.get("models", []):
            raw = m.get("name", "")
            if raw and raw not in names:
                names.append(raw)
        return sorted(names)
    except Exception:
        return []


def is_ollama_running(
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 3,
) -> bool:
    """Return True if Ollama is reachable at the given endpoint."""
    try:
        urllib.request.urlopen(f"{endpoint}/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def get_vision_models(
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 5,
) -> List[str]:
    """Return only vision-capable installed models."""
    installed = list_installed_models(endpoint, timeout)
    return [
        m for m in installed
        if any(v in m.lower() for v in _VISION_CAPABLE_SUBSTRINGS)
    ]


def get_combined_model_list(
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 5,
) -> List[str]:
    """Return installed models first, then popular uninstalled models (deduped).

    Suitable for populating a Combobox — installed models appear at the top.
    """
    installed = list_installed_models(endpoint, timeout)
    combined = list(installed)
    for m in OLLAMA_POPULAR_MODELS:
        bare = m.split(":")[0]
        if bare not in combined and m not in combined:
            combined.append(m)
    return combined


def get_model_info(
    model_name: str,
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 10,
) -> dict:
    """Query Ollama /api/show for model metadata.

    Returns dict with keys: name, parameters, template, details, etc.
    Returns {"error": reason} if unavailable.
    """
    try:
        url = f"{endpoint}/api/show"
        body = json.dumps({"name": model_name}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def is_vision_capable(model_name: str) -> bool:
    """Heuristic check: is this model name likely vision-capable?"""
    lower = model_name.lower()
    return any(v in lower for v in _VISION_CAPABLE_SUBSTRINGS)
