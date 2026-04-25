#!/usr/bin/env python3
"""
Subprocess CLI adapter — shells out to any AI CLI binary.

Config keys:
  executable  (str)   Path or name of the CLI binary (no default — must be set)
  args        (list)  Extra CLI flags prepended before the prompt (default: [])
  timeout     (int)   Seconds before subprocess is killed (default: 300)
  cwd         (str)   Working directory for the subprocess (default: project dir or cwd)

See adapters/README.md for provider configuration examples.
"""

import os
import shutil
import subprocess
from pathlib import Path
from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


class SubprocessCliAdapter(BaseAdapter):
    """Adapter that shells out to any AI CLI binary via subprocess."""

    def send(self, request: dict) -> dict:
        executable = (self.config.get("executable") or "").strip()
        if not executable:
            return _error_response(
                "SubprocessCliAdapter: 'executable' not set in provider config."
            )

        # Validate executable: must be findable on PATH or be an existing file
        resolved = shutil.which(executable)
        if resolved is None and not Path(executable).is_file():
            return _error_response(
                f"SubprocessCliAdapter: executable '{executable}' not found on PATH or filesystem."
            )
        safe_executable = resolved or executable

        prompt      = self._build_prompt(request)
        full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        extra_args  = self.config.get("args", [])
        timeout     = self.config.get("timeout", 300)
        cwd         = self.config.get("cwd") or None

        # Pass the prompt via stdin to avoid Windows 32KB command-line length limit.
        # Claude CLI: `claude --dangerously-skip-permissions -p` reads from stdin when
        # no prompt argument is provided.
        cmd = [safe_executable] + list(extra_args)

        try:
            result = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            output = result.stdout.strip()
            if not output and result.stderr.strip():
                return _error_response(
                    f"CLI returned no stdout. stderr: {result.stderr[:500]}"
                )
            return self._parse_response(output)

        except subprocess.TimeoutExpired:
            return _error_response(f"CLI timed out after {timeout}s.")
        except FileNotFoundError:
            return _error_response(
                f"CLI binary '{executable}' not found. Ensure it is on PATH."
            )
        except Exception as exc:
            return _error_response(f"CLI error: {exc}")
