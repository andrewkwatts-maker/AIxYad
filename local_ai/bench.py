"""Measure llama-server profiles the way coding agents use them.

    python -m local_ai.bench dense-gpu dense-gpu-ub2048      # load, measure, unload each
    python -m local_ai.bench --probe dense-gpu-c128k-q4      # projected VRAM only, no load
    python -m local_ai.bench --list

Each profile is loaded on its own, measured, and stopped, so only one copy of the model
is ever in memory. Results append to ``<out>/results.jsonl`` and print as a table.

Tests (raw /completion calls, greedy, fixed lengths, prompt cache off):
  decode      one request, 512-token prompt, 192 new tokens (second of two runs)
  prefill     one 4096-token prompt
  parallel    4 requests at once, 512-token prompts, 192 new tokens each (total over wall time)
  deep        one prompt of up to 16384 tokens (what the slot allows), then 128 new tokens

Each result also records VRAM requested (from the load log) against VRAM really held;
`gpu_spill_mib` above ~200 means the driver moved part of it to system RAM.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from local_ai.llama_server import LlamaServer, ServerStartError, gpu_dedicated_mib, gpu_memory_used_mib
from local_ai.profiles import ServerProfile, load_profiles

CORPUS_ROOT = Path(__file__).resolve().parents[1]
ALL_TESTS = ("decode", "prefill", "parallel", "deep")


# -- HTTP helpers ------------------------------------------------------------
def _post(base_url: str, path: str, body: dict, timeout: float = 1800) -> dict:
    request = urllib.request.Request(
        base_url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.load(reply)


def complete(base_url: str, tokens: list[int], n_predict: int) -> dict:
    """One greedy completion of exactly n_predict tokens; returns llama-server's timings."""
    reply = _post(base_url, "/completion", {
        "prompt": tokens, "n_predict": n_predict, "temperature": 0, "ignore_eos": True,
        "cache_prompt": False, "stream": False,
    })
    return reply["timings"]


def corpus_tokens(base_url: str, needed: int, cache_dir: Path) -> list[int]:
    """Real source code from this repo, tokenized by the server, at least `needed` tokens.

    Only a few known folders are read (no recursive walk: the repo may sit on a slow
    disk), and the tokens are cached per model."""
    alias = json.load(urllib.request.urlopen(base_url + "/v1/models", timeout=30))["data"][0]["id"]
    cache = cache_dir / f"corpus-{alias}.json"
    if cache.is_file():
        tokens = json.loads(cache.read_text(encoding="utf-8"))
        if len(tokens) >= needed:
            return tokens
    files = [p for folder in ("", "adapters", "local_ai", "tests") for p in sorted((CORPUS_ROOT / folder).glob("*.py"))]
    text = "".join(f"\n# file: {p.name}\n" + p.read_text(encoding="utf-8", errors="replace") for p in files)
    tokens = _post(base_url, "/tokenize", {"content": text})["tokens"]
    if len(tokens) < needed:
        raise RuntimeError(f"corpus only has {len(tokens)} tokens, need {needed}")
    cache.write_text(json.dumps(tokens[:needed]), encoding="utf-8")
    return tokens[:needed]


# -- tests -------------------------------------------------------------------
def test_decode(url: str, toks: list[int]) -> dict:
    complete(url, toks[:512], 192)                      # warm-up: page tables, expert cache
    t = complete(url, toks[512:1024], 192)
    return {"decode_tps": round(t["predicted_per_second"], 2)}


def test_prefill(url: str, toks: list[int]) -> dict:
    t = complete(url, toks[2000:2000 + 4096], 4)
    return {"prefill_tps": round(t["prompt_per_second"], 1)}


def test_parallel(url: str, toks: list[int], agents: int = 4) -> dict:
    prompts = [toks[20000 + i * 600: 20000 + i * 600 + 512] for i in range(agents)]
    started = time.perf_counter()
    with ThreadPoolExecutor(agents) as pool:
        timings = list(pool.map(lambda p: complete(url, p, 192), prompts))
    wall = time.perf_counter() - started
    total = sum(t["predicted_n"] for t in timings)
    return {  # wall clock covers queueing when there are fewer slots than agents
        "parallel_total_tps": round(total / wall, 2),
        "parallel_each_tps": round(sum(t["predicted_per_second"] for t in timings) / agents, 2),
        "parallel_wall_s": round(wall, 1),
    }


def slot_context(url: str) -> int:
    with urllib.request.urlopen(url + "/props", timeout=30) as reply:
        return int(json.load(reply)["default_generation_settings"]["n_ctx"])


DEEP_TOKENS = 16384  # set from --deep
QUIET_BELOW, QUIET_WAIT_S = 40.0, 600.0  # set from --quiet-below / --quiet-wait


def wait_for_quiet(threshold: float, max_wait_s: float) -> float | None:
    """Wait until other work on the PC uses less than `threshold`% CPU (or give up after
    `max_wait_s`), so a test measures the model and not a compile running beside it."""
    deadline = time.monotonic() + max_wait_s
    load = background_cpu_pct()
    while load is not None and load >= threshold and time.monotonic() < deadline:
        print(f"    waiting: other work is using {load:.0f}% CPU", flush=True)
        time.sleep(20)
        load = background_cpu_pct()
    return load


def test_deep(url: str, toks: list[int]) -> dict:
    depth = min(DEEP_TOKENS, slot_context(url) - 256)
    t = complete(url, toks[:depth], 128)
    return {"deep_tokens": depth, "deep_prefill_tps": round(t["prompt_per_second"], 1),
            "deep_decode_tps": round(t["predicted_per_second"], 2)}


TESTS = {"decode": test_decode, "prefill": test_prefill, "parallel": test_parallel, "deep": test_deep}


def background_cpu_pct() -> float | None:
    """Whole-machine CPU use over two seconds while the server is idle (Windows typeperf)."""
    try:
        out = subprocess.run(
            ["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "2", "-si", "1"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
        values = [float(line.rsplit(",", 1)[1].strip('"')) for line in out.splitlines()
                  if line.startswith('"') and line.count(",") == 1 and not line.startswith('"(PDH')]
        return round(sum(values) / len(values), 0) if values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# -- runners -----------------------------------------------------------------
def probe(profile: ServerProfile, log_dir: Path, timeout_s: float = 90) -> dict:
    """Start with --fit on just long enough to read its VRAM projection, then stop."""
    log_path = log_dir / f"probe-{profile.name}.log"
    log_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        # The fit's memory breakdown is only logged at verbosity 4.
        proc = subprocess.Popen([*profile.command(), "--fit", "on", "--verbosity", "4"],
                                stdout=log, stderr=subprocess.STDOUT)
    deadline, text = time.monotonic() + timeout_s, ""
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "memory_breakdown_print" in text and "CUDA0" in text.split("memory_breakdown_print", 1)[1]:
                break
            time.sleep(0.5)
        time.sleep(1)
        text = log_path.read_text(encoding="utf-8", errors="replace")
    finally:
        proc.kill()
        proc.wait()
    breakdown = [line.split("|", 1)[1].strip() for line in text.splitlines()
                 if "memory_breakdown_print" in line and "CUDA0" in line]
    return {"profile": profile.name, "first_breakdown": breakdown[0] if breakdown else "not printed"}


def measure(profile: ServerProfile, out_dir: Path, tests: tuple[str, ...]) -> dict:
    result: dict = {"profile": profile.name, "vars": profile.all_vars(),
                    "when": datetime.now().isoformat(timespec="seconds"),
                    "args": " ".join(profile.args), "vram_before_mib": gpu_memory_used_mib()}
    server = LlamaServer(profile, out_dir / "logs")
    started = time.perf_counter()
    try:
        server.start()
    except ServerStartError as error:
        result["error"] = str(error)[-600:]
        return result
    try:
        result["load_s"] = round(time.perf_counter() - started, 1)
        result["vram_loaded_mib"] = gpu_memory_used_mib()
        result.update(server.placement().summary())
        dedicated = gpu_dedicated_mib(server.pid) if server.pid else None
        result["gpu_dedicated_mib"] = dedicated
        if dedicated is not None and result.get("gpu_requested_mib"):
            result["gpu_spill_mib"] = max(0, round(float(result["gpu_requested_mib"]) - dedicated))
        toks = corpus_tokens(profile.base_url, max(34000, DEEP_TOKENS + 1000), out_dir)
        for name in tests:
            result[f"{name}_background_cpu"] = wait_for_quiet(QUIET_BELOW, QUIET_WAIT_S)
            print(f"  {profile.name}: {name} (background CPU {result[f'{name}_background_cpu']}%) ...", flush=True)
            try:
                result.update(TESTS[name](profile.base_url, toks))
            except Exception as error:  # one failed test should not lose the others
                result[f"{name}_error"] = repr(error)[:300]
        result["vram_peak_mib"] = gpu_memory_used_mib()
    finally:
        server.stop()
        time.sleep(5)  # let the driver release VRAM before the next profile
    return result


COLUMNS = ("profile", "layers_on_gpu", "gpu_requested_mib", "gpu_spill_mib", "decode_tps", "prefill_tps",
           "parallel_total_tps", "parallel_each_tps", "deep_decode_tps", "deep_prefill_tps", "load_s")


def table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in rows:
        cells = [str(row.get(c, row.get("error", "") if c == "layers_on_gpu" else "")) for c in COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    global DEEP_TOKENS, QUIET_BELOW, QUIET_WAIT_S
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profiles", nargs="*")
    parser.add_argument("--out", type=Path, default=Path.home() / ".automatica" / "bench")
    parser.add_argument("--tests", default=",".join(ALL_TESTS))
    parser.add_argument("--probe", action="store_true", help="print projected VRAM only")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="override a profile variable for every profile in this run, e.g. --set ctx=131072")
    parser.add_argument("--deep", type=int, default=DEEP_TOKENS, help="context depth for the deep test")
    parser.add_argument("--quiet-below", type=float, default=QUIET_BELOW,
                        help="before each test, wait until background CPU is below this percent")
    parser.add_argument("--quiet-wait", type=float, default=QUIET_WAIT_S, help="longest wait for quiet, seconds")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    DEEP_TOKENS, QUIET_BELOW, QUIET_WAIT_S = args.deep, args.quiet_below, args.quiet_wait
    overrides = dict(item.split("=", 1) for item in args.set)
    profiles = {name: p.with_vars({k: v for k, v in overrides.items() if k in p.vars})
                for name, p in load_profiles().items()}
    if args.list or not args.profiles:
        for name, profile in profiles.items():
            print(f"{name:24} {profile.description}" + (f"  vars {profile.vars}" if profile.vars else ""))
        return 0
    unknown = [p for p in args.profiles if p not in profiles]
    if unknown:
        parser.error(f"unknown profiles: {', '.join(unknown)}")

    if args.probe:
        for name in args.profiles:
            print(json.dumps(probe(profiles[name], args.out / "logs")))
        return 0

    tests = tuple(t for t in args.tests.split(",") if t)
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in args.profiles:
        print(f"== {name}", flush=True)
        row = measure(profiles[name], args.out, tests)
        rows.append(row)
        with (args.out / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
