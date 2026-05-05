#!/usr/bin/env python3
"""
HTTP Generate Content adapter — providers using the generateContent REST API.

Config keys:
  api_key      (str)  Literal API key (takes priority over api_key_env)
  api_key_env  (str)  Env var name to read the key from (default: "GENERATE_CONTENT_API_KEY")
  model        (str)  Model name (default: "gemini-2.0-flash")
  base_url     (str)  Override API root (default: Generative Language v1beta)
  timeout      (int)  Request timeout in seconds (default: 120)

See adapters/README.md for provider configuration examples.
"""

import json
import os
import urllib.request
import urllib.error

from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


class HttpGenerateContentAdapter(BaseAdapter):
    """Adapter for the HTTP generateContent protocol (Google Generative Language API).

    See adapters/README.md for provider configuration examples.
    """

    _DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, config: dict):
        super().__init__(config)
        env_var      = config.get("api_key_env", "GENERATE_CONTENT_API_KEY")
        self.api_key = config.get("api_key") or os.environ.get(env_var, "")
        self.model   = config.get("model") or config.get("model_name") or "gemini-2.0-flash"
        base         = (config.get("base_url") or self._DEFAULT_BASE).rstrip("/")
        self._base   = base

    def send(self, request: dict) -> dict:
        if not self.api_key:
            return _error_response(
                "API key not configured. Set the 'api_key' in provider config or "
                "export the env var named in 'api_key_env'."
            )

        prompt      = self._build_prompt(request)
        full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"

        url  = f"{self._base}/{self.model}:generateContent?key={self.api_key}"
        body = json.dumps({
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = self.config.get("timeout", 120)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())

            candidates = data.get("candidates", [])
            if not candidates:
                return _error_response(
                    f"generateContent API returned no candidates: {json.dumps(data)[:500]}"
                )
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return self._parse_response(text)

        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode() if exc.fp else ""
            return _error_response(f"generateContent HTTP {exc.code}: {body_text[:500]}")
        except urllib.error.URLError as exc:
            return _error_response(f"generateContent network error: {exc.reason}")
        except Exception as exc:
            return _error_response(f"generateContent error: {exc}")
