"""Server profiles: named llama-server launch settings read from YAML.

The packaged ``local_ai.yaml`` holds machine-independent settings. A user file
(``~/.aixyad/local_ai.yaml`` or ``$AIXYAD_LOCAL_AI``, falling back to
``$AUTOMATICA_LOCAL_AI``) supplies the paths for this machine and may add or
override profiles; it is merged over the packaged one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

import aixyad_paths

PACKAGED_CONFIG = Path(__file__).with_name("local_ai.yaml")


def _user_config_path() -> Path:
    """Default location of the user's machine-specific config file.

    Resolved lazily (not at import time) so merely importing this module
    never touches the real filesystem or triggers the ``.automatica`` ->
    ``.aixyad`` migration as a side effect.
    """
    return aixyad_paths.home() / "local_ai.yaml"


_VAR = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class ServerProfile:
    """One way of launching llama-server for one model.

    `arg_template` may hold `{name}` placeholders filled from `vars` (for example the
    context size), so one profile can be loaded with different settings. `ctx_total`
    is derived as ctx x slots: the shared context pool that --kv-unified divides.
    """

    name: str
    binary: str
    model: str
    host: str = "127.0.0.1"
    port: int = 11435
    alias: str = "local"
    arg_template: tuple[str, ...] = ()
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)
    dll_dirs: tuple[str, ...] = ()
    vars: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def all_vars(self) -> dict[str, str]:
        values = dict(self.vars)
        if values.get("ctx", "").isdigit() and values.get("slots", "").isdigit():
            values["ctx_total"] = str(int(values["ctx"]) * int(values["slots"]))
        return values

    @property
    def args(self) -> tuple[str, ...]:
        values = self.all_vars()
        return tuple(_VAR.sub(lambda m: values.get(m.group(1), m.group(0)), a) for a in self.arg_template)

    def with_vars(self, overrides: dict[str, object] | None) -> "ServerProfile":
        """This profile with some variables changed; unknown names are rejected."""
        if not overrides:
            return self
        unknown = set(overrides) - set(self.vars)
        if unknown:
            raise ValueError(f"profile '{self.name}' has no variables {sorted(unknown)}; it has {sorted(self.vars)}")
        return replace(self, vars={**self.vars, **{k: str(v) for k, v in overrides.items()}})

    def command(self) -> list[str]:
        return [
            self.binary, "-m", self.model,
            "--host", self.host, "--port", str(self.port), "--alias", self.alias,
            *self.args,
        ]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_config(user_file: Path | None = None) -> dict[str, Any]:
    """Return the packaged config with the user's file merged over it."""
    config = _read_yaml(PACKAGED_CONFIG)
    user_path = user_file or Path(aixyad_paths.env("LOCAL_AI", _user_config_path()))
    if user_path.is_file():
        config = _deep_merge(config, _read_yaml(user_path))
    return config


def _stringify(args: list[Any]) -> tuple[str, ...]:
    # YAML reads `on` / `off` as booleans; llama-server wants the words back.
    return tuple(("on" if a else "off") if isinstance(a, bool) else str(a) for a in args)


def load_profiles(user_file: Path | None = None) -> dict[str, ServerProfile]:
    """Build every profile, each inheriting the `defaults` block and its `extends` chain."""
    config = load_config(user_file)
    defaults = config.get("defaults", {})
    raw_profiles: dict[str, dict[str, Any]] = config.get("profiles", {})

    def resolve(name: str, seen: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in seen:
            raise ValueError(f"profile 'extends' cycle: {' -> '.join((*seen, name))}")
        raw = raw_profiles[name]
        parent = resolve(raw["extends"], (*seen, name)) if "extends" in raw else {}
        args = list(parent.get("args", [])) + list(raw.get("args", []))
        variables = {**(parent.get("vars") or {}), **(raw.get("vars") or {})}
        return {**parent, **raw, "args": args, "vars": variables}

    profiles = {}
    for name in raw_profiles:
        spec = {**defaults, **resolve(name)}
        if not spec.get("model"):
            raise ValueError(f"profile '{name}' has no model path; set it in {_user_config_path()}")
        profiles[name] = ServerProfile(
            name=name,
            binary=str(spec.get("binary", "llama-server")),
            model=str(spec["model"]),
            host=str(spec.get("host", "127.0.0.1")),
            port=int(spec.get("port", 11435)),
            alias=str(spec.get("alias", "local")),
            arg_template=_stringify(list(defaults.get("common_args", [])) + spec["args"]),
            description=str(spec.get("description", "")),
            env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
            dll_dirs=tuple(str(d) for d in spec.get("dll_dirs") or ()),
            vars={str(k): str(v) for k, v in (spec.get("vars") or {}).items()},
        )
    return profiles
