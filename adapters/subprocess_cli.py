#!/usr/bin/env python3
"""
Subprocess CLI adapter — shells out to any AI CLI binary.

Config keys:
  executable  (str)   Path or name of the CLI binary (no default — must be set)
  args        (list)  Extra CLI flags prepended before the prompt (default: [])
  timeout     (int)   Seconds before subprocess is killed (default: 300)
  cwd         (str)   Working directory for the subprocess (default: project dir or cwd)
  mode        (str)   "json" (default) — expect a single JSON action response
                      "agentic" — the CLI is itself an agent (e.g. Claude CLI in -p mode);
                                  send the task as plain prompt, treat exit 0 as step_done.

Agentic-mode rationale:
  Claude CLI (and similar) does its OWN multi-step tool work inside a single
  invocation and emits a freeform summary, NOT the {"action": "..."} JSON
  the protocol expects. In agentic mode we drop the JSON system prompt,
  trust the exit code as the success signal, and synthesize a step_done
  response so the protocol layer above can mark the task done.

See adapters/README.md for provider configuration examples.
"""

import shutil
import subprocess
from pathlib import Path
from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


# Hint for agentic CLIs (no JSON formatting requirement, just prose summary).
_AGENTIC_HINT = """\
You are a senior software engineer executing one task from an autonomous backlog.

You have full filesystem and shell access via your own tools — read files, edit files,
run commands as needed. Use them.

When you finish, print a short plain-text summary (1-3 lines) of:
  - which files you modified or created, and
  - whether the task is complete or what remains.

Do not ask for clarification. If something is ambiguous, make the most reasonable
senior-engineer choice and proceed.
"""


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

        mode        = (self.config.get("mode") or "json").lower()
        prompt      = self._build_prompt(request)
        extra_args  = self.config.get("args", [])
        cwd         = self.config.get("cwd") or None

        # Agentic CLIs (Claude, etc.) need longer timeouts — they do real work.
        default_timeout = 1800 if mode == "agentic" else 300
        timeout         = self.config.get("timeout", default_timeout)

        if mode == "agentic":
            # Drop the JSON-action SYSTEM_PROMPT — the CLI is itself the agent.
            full_prompt = f"{_AGENTIC_HINT}\n\n{prompt}"
        else:
            full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"

        cmd = [safe_executable] + list(extra_args)

        # Strip non-BMP characters that Windows cp1252 stdin can't encode
        safe_prompt = full_prompt.encode('utf-8', errors='replace').decode('utf-8')

        try:
            result = subprocess.run(
                cmd,
                input=safe_prompt,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                cwd=cwd,
            )
            output = result.stdout.strip()

            if mode == "agentic":
                # Trust exit code; synthesise a step_done so the protocol marks success.
                if result.returncode != 0:
                    return _error_response(
                        f"Agentic CLI exited {result.returncode}. "
                        f"stderr: {result.stderr[:400]}"
                    )
                summary = output[-800:] if output else "Task completed (no output)."
                return {
                    "action": "step_done",
                    "params": {"summary": summary},
                    "reasoning": "Agentic CLI returned exit 0; task treated as complete.",
                }

            # Default JSON-protocol path
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
