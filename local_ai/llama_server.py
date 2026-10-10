"""Run one llama-server process: start it, wait until it serves, read its placement, stop it."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from local_ai.profiles import ServerProfile

_PLACEMENT_PATTERNS = {
    "layers_on_gpu": re.compile(r"offloaded (\d+/\d+) layers to GPU"),
    "gpu_model_mib": re.compile(r"CUDA0 model buffer size =\s+([\d.]+) MiB"),
    "pinned_host_mib": re.compile(r"CUDA_Host model buffer size =\s+([\d.]+) MiB"),
    "mapped_host_mib": re.compile(r"CPU_Mapped model buffer size =\s+([\d.]+) MiB"),
    "gpu_kv_mib": re.compile(r"CUDA0 KV buffer size =\s+([\d.]+) MiB"),
    "gpu_rs_mib": re.compile(r"CUDA0 RS buffer size =\s+([\d.]+) MiB"),
    "cpu_kv_mib": re.compile(r"CPU KV buffer size =\s+([\d.]+) MiB"),
    "gpu_compute_mib": re.compile(r"CUDA0 compute buffer size =\s+([\d.]+) MiB"),
    "moe_cache": re.compile(r"(moe[ _-]cache[^\n]{0,120})", re.IGNORECASE),
    "projected_mib": re.compile(r"projected to use (\d+) MiB of device memory vs (\d+) MiB"),
}


class ServerStartError(RuntimeError):
    """llama-server exited or never became ready."""


@dataclass
class Placement:
    """Where llama-server put the model, read from its load log."""

    values: dict[str, list[str]]

    def summary(self) -> dict[str, str]:
        def total(key: str) -> str:
            nums = [float(v) for v in self.values.get(key, [])]
            return f"{sum(nums):.0f}" if nums else ""

        requested = sum(float(v) for key in ("gpu_model_mib", "gpu_kv_mib", "gpu_rs_mib", "gpu_compute_mib")
                        for v in self.values.get(key, []))
        return {
            "layers_on_gpu": (self.values.get("layers_on_gpu") or [""])[-1],
            "gpu_model_mib": total("gpu_model_mib"),
            "gpu_kv_mib": total("gpu_kv_mib"),
            "gpu_rs_mib": total("gpu_rs_mib"),
            "gpu_compute_mib": total("gpu_compute_mib"),
            "gpu_requested_mib": f"{requested:.0f}",
            "pinned_host_mib": total("pinned_host_mib"),
            "cpu_kv_mib": total("cpu_kv_mib"),
            "moe_cache": (self.values.get("moe_cache") or [""])[-1],
        }


class LlamaServer:
    """A llama-server child process bound to one profile."""

    def __init__(self, profile: ServerProfile, log_dir: Path) -> None:
        self.profile = profile
        self.log_path = log_dir / f"{profile.name}.log"
        self._process: subprocess.Popen[bytes] | None = None
        log_dir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout_s: float = 600.0) -> None:
        if _port_in_use(self.profile.host, self.profile.port):
            # Otherwise the health check would pass against someone else's server.
            raise ServerStartError(f"{self.profile.host}:{self.profile.port} is already in use by another server")
        env = {**os.environ, **self.profile.env}
        if self.profile.dll_dirs:  # e.g. the CUDA runtime a GPU build loads at start-up
            env["PATH"] = os.pathsep.join((*self.profile.dll_dirs, env.get("PATH", "")))
        with self.log_path.open("wb") as log:
            self._process = subprocess.Popen(
                self.profile.command(), stdout=log, stderr=subprocess.STDOUT, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        self._wait_ready(timeout_s)

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=30)
        self._process = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def __enter__(self) -> "LlamaServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                raise ServerStartError(f"llama-server exited with {self._process.returncode}: {self.tail()}")
            try:
                with urllib.request.urlopen(f"{self.profile.base_url}/health", timeout=5) as reply:
                    if reply.status == 200 and json.load(reply).get("status") == "ok":
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(2)
        self.stop()
        raise ServerStartError(f"llama-server not ready after {timeout_s:.0f}s: {self.tail()}")

    # -- inspection --------------------------------------------------------
    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def tail(self, lines: int = 15) -> str:
        return "\n".join(self.log_text().splitlines()[-lines:])

    def placement(self) -> Placement:
        text = self.log_text()
        found: dict[str, list[str]] = {}
        for key, pattern in _PLACEMENT_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                found[key] = [m if isinstance(m, str) else "/".join(m) for m in matches]
        return Placement(found)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


def gpu_memory_used_mib() -> int | None:
    """VRAM in use on GPU 0, from nvidia-smi; None when it is unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        return int(out.splitlines()[0].strip())
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def gpu_dedicated_mib(pid: int) -> int | None:
    """VRAM a process really holds (Windows counter). Less than it asked for means the
    driver's sysmem fallback has moved the rest into system RAM over PCIe."""
    try:
        out = subprocess.run(
            ["typeperf", rf"\GPU Process Memory(pid_{pid}_*)\Dedicated Usage", "-sc", "1"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
        rows = [line for line in out.splitlines() if line.startswith('"') and not line.startswith('"(PDH')]
        values = [float(v.strip('"')) for v in rows[-1].split(",")[1:] if v.strip('"')]
        return round(sum(values) / 2**20) if values else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
