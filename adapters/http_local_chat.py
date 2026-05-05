#!/usr/bin/env python3
"""
Ollama adapter — local models via the Ollama HTTP API with CLI fallback.

Config keys:
  endpoint    (str)   Ollama server URL (default: "http://localhost:11434")
  model       (str)   Model name (default: "llama3.2")
  num_predict (int)   Max tokens (default: 2048)
  temperature (float) Sampling temperature (default: 0.1)
  timeout     (int)   Request timeout in seconds (default: 300)
"""

import json
import random
import subprocess
import urllib.request
import urllib.error
import sys
from typing import List, Optional

from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


class HttpLocalChatAdapter(BaseAdapter):
    """Adapter for local inference servers using the Ollama HTTP protocol."""

    MAX_RETRIES = 3

    def __init__(self, config: dict):
        super().__init__(config)
        self.endpoint = config.get("endpoint", "http://localhost:11434")
        self.model    = config.get("model", "llama3.2")
        self._messages: List[dict] = []

    def send(self, request: dict) -> dict:
        prompt = self._build_prompt(request)

        for attempt in range(self.MAX_RETRIES):
            if not self._messages:
                self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

            msg = prompt
            if attempt > 0:
                nonce = random.randint(1000, 9999)
                msg = (
                    f"{prompt}\n\n[RETRY attempt {attempt + 1}/{self.MAX_RETRIES}. "
                    f"You MUST respond with ONLY a valid JSON object with 'action' and 'params' keys. "
                    f"Nonce: {nonce}]"
                )

            self._messages.append({"role": "user", "content": msg})

            result = self._send_http()
            if result is None:
                result = self._send_cli(msg)

            if result is None:
                self._messages.pop()
                if attempt < self.MAX_RETRIES - 1:
                    continue
                return _error_response("Ollama: both HTTP API and CLI failed after retries.")

            self._messages.append({"role": "assistant", "content": result})

            response = self._parse_response(result)
            if not self._is_parse_error(response):
                return response

            raw_preview = (result or "")[:200].encode("ascii", errors="replace").decode("ascii")
            print(
                f"[OllamaAdapter] JSON parse fail attempt {attempt+1}: {repr(raw_preview)}",
                file=sys.stderr, flush=True,
            )
            if attempt < self.MAX_RETRIES - 1:
                self._messages.append({
                    "role": "system",
                    "content": (
                        f"Your previous response was not valid JSON. "
                        f"Respond ONLY with a single JSON object. "
                        f"If writing Python code in 'content', use SINGLE QUOTES for all strings "
                        f"inside the code to avoid breaking the JSON. "
                        f"Attempt {attempt + 2}/{self.MAX_RETRIES}."
                    ),
                })

        return _error_response(f"Failed to get valid JSON after {self.MAX_RETRIES} attempts.")

    def _send_http(self) -> Optional[str]:
        # Estimate token count (rough: chars / 3.5); trim history if over budget
        _max_prompt_tokens = self.config.get("max_prompt_tokens", 3000)
        _msgs = self._messages
        _estimated = sum(len(m.get("content", "")) for m in _msgs) // 3
        if _estimated > _max_prompt_tokens and len(_msgs) > 3:
            # Keep system message + last 4 exchanges; discard middle history
            _msgs = _msgs[:1] + _msgs[-8:]
            print(
                f"[OllamaAdapter] Trimmed history: ~{_estimated} tokens > {_max_prompt_tokens}; "
                f"kept {len(_msgs)} messages",
                file=__import__('sys').stderr, flush=True,
            )

        url  = f"{self.endpoint}/api/chat"
        # `format: "json"` forces the Ollama runtime to emit syntactically valid
        # JSON — without it small/code-tuned models like deepseek-coder happily
        # return prose and python code blocks that fail every JSON-parse strategy
        # downstream. Enabled by default; opt out via config_json.use_json_format=false.
        _payload = {
            "model": self.model,
            "messages": _msgs,
            "stream": False,
            "options": {
                "num_predict": self.config.get("num_predict", 2048),
                "temperature": self.config.get("temperature", 0.1),
            },
        }
        if self.config.get("use_json_format", True):
            _payload["format"] = "json"
        body = json.dumps(_payload).encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = self.config.get("timeout", 300)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                return data.get("message", {}).get("content", "")
        except Exception:
            return None

    def _send_cli(self, prompt: str) -> Optional[str]:
        full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        timeout     = self.config.get("timeout", 300)
        try:
            result = subprocess.run(
                ["ollama", "run", self.model, full_prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout.strip() if result.stdout.strip() else None
        except Exception:
            return None

    def send_vision(self, image_base64: str, prompt: str,
                    model: Optional[str] = None) -> Optional[str]:
        """Send an image + text prompt to an Ollama vision model."""
        target_model = model or self.model
        body = json.dumps({
            "model": target_model,
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [image_base64],
            }],
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{self.endpoint}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = self.config.get("timeout", 300)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                return data.get("message", {}).get("content", "")
        except Exception:
            return None

    # Method aliases
    _send_vision_http = send_vision
    
    def generate(self, request: dict) -> str:
        """Simple text generation without full protocol overhead (for planning phase).
        
        Args:
            request: dict with 'prompt', optional 'max_tokens', 'temperature'
        
        Returns:
            Raw text response from the model
        """
        url = f"{self.endpoint}/api/generate"
        body = json.dumps({
            "model": self.model,
            "prompt": request.get("prompt", ""),
            "stream": False,
            "options": {
                "num_predict": request.get("max_tokens", 2048),
                "temperature": request.get("temperature", 0.7),
            },
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
                return data.get("response", "")
        except Exception as exc:
            return f"Error: {exc}"
