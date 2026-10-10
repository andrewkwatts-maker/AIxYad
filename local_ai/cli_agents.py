"""Launch a coding CLI headless against the gateway and turn its JSON stream into events.

Each CLI runs with its own config folder, so the user's normal CLI settings, sign-in
and history are never touched, and with the gateway's address and token passed only in
the child's environment. The prompt goes in on stdin (no command-line length limit).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Read-only tools for planning; workers also get the edit tools.
PLANNER_TOOLS = ("Read", "Grep", "Glob", "LS")
WORKER_TOOLS = (*PLANNER_TOOLS, "Edit", "MultiEdit", "Write", "TodoWrite")


@dataclass(frozen=True)
class AgentCommand:
    argv: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class AgentEvent:
    kind: str           # text | tool_use | tool_result | status | result | error | stderr
    text: str = ""
    data: Any = None


def _resolve(binary: str) -> str:
    # npm installs `claude` as claude.cmd on Windows; subprocess needs the real file.
    return shutil.which(binary) or binary


def _base_env(drop: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in drop}


def claude_command(
    *, binary: str, gateway_url: str, token: str, route: str, planner_route: str,
    worker_route: str, config_dir: Path, read_only: bool, allow_shell: bool,
) -> AgentCommand:
    tools = list(PLANNER_TOOLS if read_only else WORKER_TOOLS)
    if allow_shell and not read_only:
        tools.append("Bash")
    argv = [_resolve(binary), "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "dontAsk" if read_only else "acceptEdits",
            "--allowedTools", ",".join(tools)]
    if not allow_shell or read_only:
        argv += ["--disallowedTools", "Bash"]
    config_dir.mkdir(parents=True, exist_ok=True)
    env = _base_env(("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE"))
    env.update({
        "ANTHROPIC_BASE_URL": gateway_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": route,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": planner_route,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": route,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": worker_route,
        "CLAUDE_CODE_SUBAGENT_MODEL": worker_route,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        # A changing attribution header would defeat the server's prompt cache every turn.
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    })
    return AgentCommand(argv, env)


def qwen_command(
    *, binary: str, gateway_url: str, token: str, route: str, config_dir: Path,
    read_only: bool, allow_shell: bool,
) -> AgentCommand:
    """Qwen Code (OpenAI-style). Untested here: Qwen Code was not installed when written."""
    argv = [_resolve(binary), "-p", "--output-format", "stream-json"]
    if not read_only:
        argv += ["--approval-mode", "auto-edit"]
    config_dir.mkdir(parents=True, exist_ok=True)
    env = _base_env(("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"))
    env.update({
        "OPENAI_BASE_URL": gateway_url + "/v1",
        "OPENAI_API_KEY": token,
        "OPENAI_MODEL": route,
        "QWEN_HOME": str(config_dir),
    })
    return AgentCommand(argv, env)


def _content(message: dict) -> list[dict]:
    body = message.get("message", message)
    content = body.get("content")
    return content if isinstance(content, list) else []


def _short(value: Any, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def parse_line(line: str) -> list[AgentEvent]:
    """Map one stream-json line (Claude Code's format) to zero or more events."""
    line = line.strip()
    if not line:
        return []
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return [AgentEvent("text", line)]
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        return [AgentEvent("status", f"agent started ({message.get('model', '?')})")]
    if kind == "assistant":
        events = []
        for block in _content(message):
            if block.get("type") == "text" and block.get("text"):
                events.append(AgentEvent("text", block["text"]))
            elif block.get("type") == "tool_use":
                events.append(AgentEvent("tool_use", block.get("name", ""),
                                         {"name": block.get("name"), "input": block.get("input")}))
        return events
    if kind == "user":
        return [AgentEvent("tool_result", _short(block.get("content", "")),
                           {"is_error": bool(block.get("is_error"))})
                for block in _content(message) if block.get("type") == "tool_result"]
    if kind == "result":
        return [AgentEvent("error" if message.get("is_error") else "result",
                           str(message.get("result", "")),
                           {k: message.get(k) for k in ("num_turns", "usage", "duration_ms")})]
    return []
