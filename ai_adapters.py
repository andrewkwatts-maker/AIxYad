#!/usr/bin/env python3
"""
Backward-compatibility shim — all adapter code now lives in the adapters/ package.

Existing code that imports from ai_adapters continues to work unchanged:
    from ai_adapters import get_adapter, HttpLocalChatAdapter, VALID_ACTIONS
"""

from adapters import (  # noqa: F401 — re-exported for backward compat
    BaseAdapter,
    SYSTEM_PROMPT,
    VALID_ACTIONS,
    _extract_json,
    _error_response,
    # Protocol-named classes
    HttpLocalChatAdapter,
    HttpGenerateContentAdapter,
    HttpChatCompletionsAdapter,
    SubprocessCliAdapter,
    # Legacy aliases (kept so existing imports don't break)
    OllamaAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    ClaudeAdapter,
    # Functions
    get_adapter,
    get_available_adapter_types,
)
