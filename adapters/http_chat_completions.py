#!/usr/bin/env python3
"""
HTTP Chat Completions adapter — any provider that speaks the /v1/chat/completions protocol.

This is a de-facto open standard implemented by many inference providers.
See adapters/README.md for example provider configurations.

Config keys (all come from the DB provider record — no subclassing needed):
  base_url     (str)  API root or full completions URL
  model        (str)  Model name passed to the API
  model_name   (str)  Alias for model (whichever is set in DB)
  api_key      (str)  Literal API key (takes priority over api_key_env)
  api_key_env  (str)  Env var name to read the key from
  timeout      (int)  Request timeout in seconds (default: 120)

Endpoint resolution:
  base_url already ends with /chat/completions  →  used as-is
  base_url ends with /v1                        →  /chat/completions appended
  otherwise                                     →  /v1/chat/completions appended
"""

import json
import os
import urllib.request
import urllib.error
from typing import List

from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


class HttpChatCompletionsAdapter(BaseAdapter):
    """Adapter for any provider using the HTTP chat completions protocol."""

    def __init__(self, config: dict):
        super().__init__(config)
        base = (config.get("base_url") or "https://api.openai.com").rstrip("/")
        if base.endswith("/chat/completions"):
            self.endpoint = base
        elif base.endswith("/v1"):
            self.endpoint = f"{base}/chat/completions"
        else:
            self.endpoint = f"{base}/v1/chat/completions"

        self.model = config.get("model") or config.get("model_name") or "gpt-4o"

        env_var       = config.get("api_key_env", "")
        self.api_key  = config.get("api_key") or (
            os.environ.get(env_var, "") if env_var else ""
        )
        self._env_var = env_var or "API_KEY"
        self._messages: List[dict] = []

    def send(self, request: dict) -> dict:
        if not self.api_key:
            return _error_response(
                f"API key not set. Export the '{self._env_var}' environment variable "
                f"or add 'api_key' to the provider config."
            )

        prompt = self._build_prompt(request)

        if not self._messages:
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})
        self._messages.append({"role": "user", "content": prompt})

        body = json.dumps({
            "model": self.model,
            "messages": self._messages,
            "response_format": {"type": "json_object"},
        }).encode()

        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        timeout = self.config.get("timeout", 120)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())

            text = data["choices"][0]["message"]["content"]
            self._messages.append({"role": "assistant", "content": text})
            return self._parse_response(text)

        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode() if exc.fp else ""
            return _error_response(f"HTTP {exc.code}: {body_text[:500]}")
        except urllib.error.URLError as exc:
            return _error_response(
                f"Network error connecting to {self.endpoint}: {exc.reason}"
            )
        except (KeyError, IndexError) as exc:
            return _error_response(
                f"Unexpected response format from {self.endpoint}: {exc}"
            )
        except Exception as exc:
            return _error_response(f"Provider error ({self.endpoint}): {exc}")
