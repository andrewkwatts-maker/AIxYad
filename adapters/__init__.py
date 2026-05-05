#!/usr/bin/env python3
"""
Automatica AI Adapter registry.

Add a new adapter by:
  1. Creating  adapters/<your_adapter>.py  with a class that extends BaseAdapter.
  2. Importing it below and adding a key to _ADAPTER_MAP.

No other file needs to change.
"""

from ._base import (
    BaseAdapter,
    SYSTEM_PROMPT,
    VALID_ACTIONS,
    _extract_json,
    _error_response,
)
from .http_local_chat        import HttpLocalChatAdapter
from .http_generate_content  import HttpGenerateContentAdapter
from .http_chat_completions  import HttpChatCompletionsAdapter
from .subprocess_cli         import SubprocessCliAdapter

# Backward-compat aliases for existing code / DB rows that use old class names
OllamaAdapter            = HttpLocalChatAdapter
GeminiAdapter            = HttpGenerateContentAdapter
OpenAICompatibleAdapter  = HttpChatCompletionsAdapter
ClaudeAdapter            = SubprocessCliAdapter


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ADAPTER_MAP: dict = {
    # Protocol-named primary keys
    "http_chat_completions":  HttpChatCompletionsAdapter,   # /v1/chat/completions (OpenAI-format)
    "http_generate_content":  HttpGenerateContentAdapter,   # Google generateContent API
    "http_local_chat":        HttpLocalChatAdapter,         # Ollama /api/chat (local models)
    "subprocess_cli":         SubprocessCliAdapter,         # Any AI CLI binary via subprocess
    # Backward-compat aliases — existing DB rows continue to work
    "openai_compatible":      HttpChatCompletionsAdapter,
    "openai":                 HttpChatCompletionsAdapter,
    "grok":                   HttpChatCompletionsAdapter,
    "gemini":                 HttpGenerateContentAdapter,
    "ollama":                 HttpLocalChatAdapter,
    "claude":                 SubprocessCliAdapter,
}

# Primary types shown in the UI (aliases excluded)
_PRIMARY_ADAPTER_TYPES = [
    "http_chat_completions",
    "http_generate_content",
    "http_local_chat",
    "subprocess_cli",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_adapter(provider: str, config: dict) -> BaseAdapter:
    """Return an initialised adapter for *provider* using *config*.

    Args:
        provider: Adapter type key (e.g. ``"http_local_chat"``,
                  ``"http_chat_completions"``).  Case-insensitive; legacy
                  aliases like ``"ollama"`` and ``"openai"`` are accepted.
        config:   Provider-specific configuration dict.  Keys vary by adapter
                  (see each adapter module's docstring or adapters/README.md).

    Raises:
        ValueError: If *provider* is not in the registry.
    """
    key = provider.lower().strip()
    cls = _ADAPTER_MAP.get(key)
    if cls is None:
        supported = ", ".join(sorted(_ADAPTER_MAP))
        raise ValueError(
            f"Unknown adapter type '{provider}'. "
            f"Registered types: {supported}"
        )
    return cls(config)


def get_available_adapter_types() -> list:
    """Return the primary adapter type keys that have code implementations.

    These are the valid values for ``provider_type`` when adding a provider
    via the UI or CLI.  Derived from code — never from config or hardcoded
    provider lists.
    """
    return list(_PRIMARY_ADAPTER_TYPES)


__all__ = [
    "BaseAdapter",
    "SYSTEM_PROMPT",
    "VALID_ACTIONS",
    "_extract_json",
    "_error_response",
    # New protocol-named classes
    "HttpLocalChatAdapter",
    "HttpGenerateContentAdapter",
    "HttpChatCompletionsAdapter",
    "SubprocessCliAdapter",
    # Backward-compat aliases
    "OllamaAdapter",
    "GeminiAdapter",
    "OpenAICompatibleAdapter",
    "ClaudeAdapter",
    # Functions
    "get_adapter",
    "get_available_adapter_types",
]
