"""Gateway settings read from the `gateway:` section of local_ai.yaml.

A *backend* is one model server (local or on the LAN). A *route* is a role such as
`planner` or `worker` that clients name in the request's `model` field; it points at a
backend and carries request defaults. Backends that share a GPU form a *group*: only one
member is loaded at a time and the scheduler swaps between them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aixyad_paths
from local_ai.profiles import ServerProfile, load_config, load_profiles


@dataclass(frozen=True)
class BackendSpec:
    name: str
    url: str
    model: str
    slots: int = 1
    group: str | None = None
    profile: str | None = None
    api_key: str | None = field(default=None, repr=False)
    health_path: str = "/health"   # Ollama has none: use "/" for it

    @property
    def managed(self) -> bool:
        """True when the gateway starts and stops this server itself."""
        return self.profile is not None


@dataclass(frozen=True)
class RouteSpec:
    name: str
    backend: str
    thinking: bool | None = None
    max_tokens: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobsSpec:
    claude_bin: str = "claude"
    qwen_bin: str = "qwen"
    branch_prefix: str = "claude/local-ai/"
    worktree_root: str = "{repo_parent}/{repo_name}.agents"
    # default_factory: resolved per-instance, not once at class-definition
    # (import) time — merely importing this module must never touch the
    # real filesystem or trigger the .automatica -> .aixyad migration.
    cli_config_root: Path = field(default_factory=lambda: aixyad_paths.home() / "cli")
    state_dir: Path = field(default_factory=lambda: aixyad_paths.home() / "jobs")
    step_timeout_s: float = 3600.0
    max_plan_steps: int = 8


@dataclass(frozen=True)
class GatewaySpec:
    listen: tuple[tuple[str, int], ...]
    token_file: Path
    default_route: str
    backends: dict[str, BackendSpec]
    routes: dict[str, RouteSpec]
    profiles: dict[str, ServerProfile]
    jobs: JobsSpec
    max_hold_s: float = 120.0
    idle_unload_s: float = 0.0
    queue_timeout_s: float = 1800.0

    def route_for(self, model: str | None) -> tuple[RouteSpec | None, BackendSpec]:
        """Resolve a request's `model` field to (route, backend)."""
        if model in self.routes:
            route = self.routes[model]
            return route, self.backends[route.backend]
        for backend in self.backends.values():
            if model == backend.model:
                return None, backend
        route = self.routes[self.default_route]
        return route, self.backends[route.backend]


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _api_key(raw: dict[str, Any]) -> str | None:
    if raw.get("api_key_env"):
        return os.environ.get(raw["api_key_env"]) or None
    if raw.get("api_key_file"):
        key_path = _expand(raw["api_key_file"])
        return key_path.read_text(encoding="utf-8").strip() if key_path.is_file() else None
    return None


def _parallel_slots(args: tuple[str, ...]) -> int:
    """llama-server's --parallel / -np value (the last one wins, as in llama-server)."""
    slots = 1
    for flag, value in zip(args, args[1:]):
        if flag in ("--parallel", "-np") and value.isdigit():
            slots = int(value)
    return slots


def _parse_listen(items: list[str]) -> tuple[tuple[str, int], ...]:
    parsed = []
    for item in items:
        host, _, port = str(item).rpartition(":")
        parsed.append((host or "127.0.0.1", int(port)))
    return tuple(parsed)


def load_gateway(user_file: Path | None = None) -> GatewaySpec:
    config = load_config(user_file)
    profiles = load_profiles(user_file)
    raw = config.get("gateway") or {}

    backends: dict[str, BackendSpec] = {}
    for name, spec in (raw.get("backends") or {}).items():
        profile = profiles.get(spec["profile"]) if spec.get("profile") else None
        if spec.get("profile") and profile is None:
            raise ValueError(f"backend '{name}' uses unknown profile '{spec['profile']}'")
        url = spec.get("url") or (profile.base_url if profile else None)
        slots = spec.get("slots") or (_parallel_slots(profile.args) if profile else 1)
        model = spec.get("model") or (profile.alias if profile else None)
        if not url or not model:
            raise ValueError(f"backend '{name}' needs a url and model (or a profile)")
        backends[name] = BackendSpec(
            name=name, url=url.rstrip("/"), model=model, slots=int(slots),
            group=spec.get("group"), profile=spec.get("profile"), api_key=_api_key(spec),
            health_path=spec.get("health_path", "/health"),
        )

    routes: dict[str, RouteSpec] = {}
    for name, spec in (raw.get("routes") or {}).items():
        if spec.get("backend") not in backends:
            raise ValueError(f"route '{name}' points at unknown backend '{spec.get('backend')}'")
        routes[name] = RouteSpec(
            name=name, backend=spec["backend"], thinking=spec.get("thinking"),
            max_tokens=spec.get("max_tokens"), params=dict(spec.get("params") or {}),
        )

    default_route = raw.get("default_route", "planner")
    if default_route not in routes:
        raise ValueError(f"default_route '{default_route}' is not a route")

    jobs_raw = raw.get("jobs") or {}
    # cli_config_root / state_dir default to aixyad_paths.home() sub-paths, resolved
    # here (not as a class-level default) so loading a gateway spec that overrides
    # both never has to touch the real home directory.
    _cli_config_root_raw = jobs_raw.get("cli_config_root")
    _state_dir_raw = jobs_raw.get("state_dir")
    jobs = JobsSpec(
        claude_bin=jobs_raw.get("claude_bin", JobsSpec.claude_bin),
        qwen_bin=jobs_raw.get("qwen_bin", JobsSpec.qwen_bin),
        branch_prefix=jobs_raw.get("branch_prefix", JobsSpec.branch_prefix),
        worktree_root=jobs_raw.get("worktree_root", JobsSpec.worktree_root),
        cli_config_root=_expand(_cli_config_root_raw) if _cli_config_root_raw
                        else aixyad_paths.home() / "cli",
        state_dir=_expand(_state_dir_raw) if _state_dir_raw
                  else aixyad_paths.home() / "jobs",
        step_timeout_s=float(jobs_raw.get("step_timeout_s", JobsSpec.step_timeout_s)),
        max_plan_steps=int(jobs_raw.get("max_plan_steps", JobsSpec.max_plan_steps)),
    )

    swap = raw.get("swap") or {}
    return GatewaySpec(
        listen=_parse_listen(raw.get("listen") or ["127.0.0.1:8770"]),
        token_file=_expand(raw.get("token_file", "~/.aixyad/gateway.token")),
        default_route=default_route, backends=backends, routes=routes, profiles=profiles,
        jobs=jobs, max_hold_s=float(swap.get("max_hold_s", 120)),
        idle_unload_s=float(swap.get("idle_unload_s", 0)),
        queue_timeout_s=float(raw.get("queue_timeout_s", 1800)),
    )
