#!/usr/bin/env python3
"""
Subprocess CLI adapter — shells out to any AI CLI binary.

Config keys:
  executable          (str)   Path or name of the CLI binary (no default — must be set)
  args                (list)  Extra CLI flags prepended before the prompt (default: [])
  timeout             (int)   Seconds before subprocess is killed (default: 300, or 1800 in agentic mode)
  cwd                 (str)   Working directory for the subprocess (default: project dir or cwd)
  mode                (str)   "json" (default) — expect a single JSON action response
                              "agentic" — the CLI is itself an agent (e.g. Claude CLI in -p mode)
  expects_changes     (bool)  agentic-only. Default True. When True, exit-0 + zero-diff
                              + no-op summary phrases ⇒ false-success → return need_help.
                              Set False for legit investigation/audit tasks.
  hang_grace_seconds  (int)   agentic-only. Default 300 (5 min). Don't kill before this.
  hang_window_seconds (int)   agentic-only. Default 90. CPU-flat-line window.
  hang_cpu_threshold  (float) agentic-only. Default 0.5. CPU seconds used in window
                              to be considered "alive".

Agentic-mode rationale:
  Claude CLI (and similar) does its OWN multi-step tool work inside a single
  invocation and emits a freeform summary, NOT the {"action": "..."} JSON the
  protocol expects. In agentic mode we drop the JSON system prompt, trust the
  exit code as the success signal, and synthesise a step_done response so the
  protocol layer above can mark the task done.

Three guardrails (added 2026-05-03 after observed bugs):
  WF-5: full stderr/stdout dumped to logs/cli_failures/<ts>.log on every
        non-success exit (head-tail snippets are kept inline for the DB).
  WF-7: liveness watchdog — sample child CPU every hang_window_seconds. If
        growth < hang_cpu_threshold for 2 consecutive windows past the grace
        period, kill the process tree (not just the parent — Claude spawns
        cmd.exe → node.exe).
  WF-8: false-success detection — after exit 0 in agentic mode, check git
        diff in cwd. If no files changed AND summary contains no-op phrases,
        return need_help instead of step_done.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from ._base import BaseAdapter, SYSTEM_PROMPT, _error_response


log = logging.getLogger(__name__)


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

# Two distinct buckets of "no files changed" outcomes — they must NOT be
# conflated. The first means "the work is genuinely already in place"
# (treat as step_done). The second means "I cannot proceed" (treat as
# need_help → WF-4 classifies as refusal).
_VERIFIED_COMPLETE_PHRASES = [
    "already done", "already complete", "already implemented", "already in place",
    "already fixed", "already present", "already wired",
    "no changes needed", "no changes required", "no action needed",
    "no files needed", "no new files needed",
    "satisfy ", "satisfies ", "meet the criteria", "meets the criteria",
    "task appears complete", "appears complete from",
    "existing files already", "existing implementation already",
]
_BLOCKED_PHRASES = [
    "could not", "cannot proceed", "cannot complete", "cannot implement",
    "cannot do this", "cannot do that", "can't proceed", "can't complete",
    "unable to proceed", "unable to complete", "unable to implement",
    "would need", "i would need to",
    "requires more", "requires additional",
    "i don't have", "i do not have",
    "not enough information", "not enough context",
    "have not inspected", "haven't inspected",
    "need to know", "need to inspect",
]


# ── Logging helpers ─────────────────────────────────────────────────────────

def _failure_log_path() -> Path:
    """Where per-failure full stderr/stdout dumps live."""
    base = Path(os.environ.get("AUTOMATICA_HOME") or
                Path(__file__).resolve().parents[1])
    d = base / "logs" / "cli_failures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_failure_log(executable: str, returncode: int,
                       stdout: str, stderr: str, prompt: str) -> Path:
    """WF-5: dump full failure context to a per-task log file."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = _failure_log_path() / f"{ts}_exit{returncode}.log"
    try:
        with path.open("w", encoding="utf-8", errors="replace") as f:
            f.write(f"# CLI failure log\n")
            f.write(f"timestamp:  {datetime.now().isoformat()}\n")
            f.write(f"executable: {executable}\n")
            f.write(f"returncode: {returncode}\n")
            f.write(f"\n## prompt (first 4K)\n{prompt[:4000]}\n")
            f.write(f"\n## stdout\n{stdout}\n")
            f.write(f"\n## stderr\n{stderr}\n")
        # FIFO eviction: keep only newest 200 files (configurable cap).
        files = sorted(_failure_log_path().glob("*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[200:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as exc:
        log.warning("could not write failure log: %s", exc)
    return path


# ── Liveness watchdog (WF-7) ────────────────────────────────────────────────

def _proc_cpu_seconds(pid: int) -> float:
    """Return total CPU time consumed by *pid* and its descendants."""
    try:
        import psutil
        p = psutil.Process(pid)
        total = sum(p.cpu_times()[:2])  # user + system, parent
        for child in p.children(recursive=True):
            try:
                t = child.cpu_times()
                total += t.user + t.system
            except Exception:
                pass
        return total
    except Exception:
        return 0.0


def _kill_tree(pid: int) -> None:
    """Kill *pid* and all its descendants (Claude → cmd.exe → node.exe on Windows)."""
    try:
        import psutil
        p = psutil.Process(pid)
        for child in p.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        try:
            p.kill()
        except Exception:
            pass
    except Exception:
        # Fallback: best-effort kill of the parent only.
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, 9)
        except Exception:
            pass


def _run_with_watchdog(cmd, prompt, timeout, cwd,
                       hang_grace_seconds, hang_window_seconds,
                       hang_cpu_threshold):
    """Run subprocess with both a wall-clock timeout AND a CPU-flatline watchdog.

    Returns (returncode, stdout, stderr, hang_killed: bool).
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )
    # Send the prompt and close stdin so the child sees EOF.
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as exc:
        log.warning("failed to write prompt to subprocess stdin: %s", exc)

    start = time.monotonic()
    last_window_end = start + hang_window_seconds
    last_window_cpu = 0.0
    flatline_streak = 0
    hang_killed = False

    while True:
        if proc.poll() is not None:
            break  # child exited

        now = time.monotonic()
        elapsed = now - start

        # Wall-clock timeout
        if elapsed > timeout:
            log.warning("subprocess wall-clock timeout (%ss); killing tree", timeout)
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            hang_killed = True
            break

        # Watchdog only after grace period
        if elapsed > hang_grace_seconds and now >= last_window_end:
            cpu_now = _proc_cpu_seconds(proc.pid)
            growth = cpu_now - last_window_cpu
            if growth < hang_cpu_threshold:
                flatline_streak += 1
                log.warning(
                    "CPU flat-line: window=%ss growth=%.2fs threshold=%.2fs streak=%d",
                    hang_window_seconds, growth, hang_cpu_threshold, flatline_streak,
                )
                if flatline_streak >= 2:
                    log.warning("HANG detected after %.0fs; killing tree", elapsed)
                    _kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    hang_killed = True
                    break
            else:
                flatline_streak = 0
            last_window_cpu = cpu_now
            last_window_end = now + hang_window_seconds

        time.sleep(2)

    stdout, stderr = "", ""
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except Exception:
        pass
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, (stdout or ""), (stderr or ""), hang_killed


# ── False-success detection (WF-8) ──────────────────────────────────────────

def _git_diff_summary(cwd: str | None) -> tuple[bool, str]:
    """Return (any_changes, short_diff_stat) for the repo at *cwd*.

    Returns (False, "") if cwd is not a git repo or git is unavailable.
    """
    if not cwd:
        return (False, "")
    try:
        # Capture both tracked changes and untracked files
        diff = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=cwd, timeout=15,
        )
        if diff.returncode != 0:
            return (False, "")
        out = diff.stdout.strip()
        return (bool(out), out[:400])
    except Exception:
        return (False, "")


def _classify_zero_diff_summary(summary: str) -> str:
    """When Claude exits 0 with no file changes, the summary tells us why.

    Returns:
      'verified_complete' — work is genuinely already in place; treat as done.
      'blocked'           — Claude cannot proceed; mark for human review.
      'unknown'           — neutral / can't tell. Default to need_help (conservative).
    """
    s = (summary or "").lower()
    has_complete = any(p in s for p in _VERIFIED_COMPLETE_PHRASES)
    has_blocked = any(p in s for p in _BLOCKED_PHRASES)
    # If both present, "blocked" wins — can't trust a confident "complete"
    # claim that comes alongside "I cannot do X".
    if has_blocked:
        return 'blocked'
    if has_complete:
        return 'verified_complete'
    return 'unknown'


# ── WF-8 v3: file-existence verification ────────────────────────────────────
# When Claude claims "verified_complete", extract the file paths it mentions
# from the prompt + summary and verify they ACTUALLY EXIST on disk. Catches
# the L0130-GEO-1 hallucination pattern: Claude said "existing files satisfy
# the spec" but the files (engine/location/GeoProjection.{hpp,cpp}) didn't
# exist. Without verification we'd trust the claim.

# Match path-like tokens with at least one '/' or '\' and a file extension.
# Skip URLs (http://) and Windows drive letters at start (handled separately).
_PATH_RE = re.compile(
    r'(?<![A-Za-z0-9_])'                         # no alphanumeric just before
    r'((?:[A-Za-z]:[\\/])?'                      # optional Windows drive
    r'[\w.\-]+(?:[\\/][\w.\-]+){1,8})'           # 2-8 path segments
    r'\.([A-Za-z0-9]{1,5})\b'                    # extension
)


def _extract_referenced_paths(*texts: str) -> list[str]:
    """Find plausible file paths in the given strings."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for m in _PATH_RE.finditer(text):
            p = m.group(0).replace("\\", "/")
            # Filter junk: URLs, very short paths, common false positives
            if p.startswith(("http://", "https://", "ftp://")):
                continue
            if len(p) < 5:
                continue
            if p.lower() in ("e.g./foo", "x/y.z"):
                continue
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _verify_referenced_files_exist(paths: list[str], cwd: str | None) -> tuple[bool, list[str], list[str]]:
    """For each path in *paths*, check existence ONLY against *cwd*-relative
    or absolute resolutions. Never falls back to Path.cwd() — that would let
    files from a sibling project (e.g. Automatica/engine/foo) accidentally
    match a Nova3D-relative reference like 'engine/foo'.

    Returns (all_exist, exists_list, missing_list).

    If *cwd* is None we cannot meaningfully verify; treat as inconclusive
    (return all_exist=False so the caller defaults to need_help).
    """
    if not paths:
        return (True, [], [])  # vacuously true
    if not cwd:
        return (False, [], list(paths))  # can't verify → conservative
    exists: list[str] = []
    missing: list[str] = []
    cwd_path = Path(cwd)
    for p in paths:
        pp = Path(p)
        candidate = pp if pp.is_absolute() else (cwd_path / pp)
        if candidate.exists():
            exists.append(p)
        else:
            missing.append(p)
    return (len(missing) == 0, exists, missing)


# ── Adapter ─────────────────────────────────────────────────────────────────

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
        expects_changes = bool(self.config.get("expects_changes", True))

        # Agentic CLIs (Claude, etc.) need longer timeouts — they do real work.
        default_timeout = 1800 if mode == "agentic" else 300
        timeout         = int(self.config.get("timeout", default_timeout))
        hang_grace      = int(self.config.get("hang_grace_seconds", 300))
        hang_window     = int(self.config.get("hang_window_seconds", 90))
        hang_threshold  = float(self.config.get("hang_cpu_threshold", 0.5))

        if mode == "agentic":
            full_prompt = f"{_AGENTIC_HINT}\n\n{prompt}"
        else:
            full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"

        cmd = [safe_executable] + list(extra_args)
        safe_prompt = full_prompt.encode("utf-8", errors="replace").decode("utf-8")

        # Capture pre-run git state for false-success detection (agentic only)
        pre_changed, _pre_stat = _git_diff_summary(cwd) if mode == "agentic" else (False, "")

        # ── Run ──────────────────────────────────────────────────────────────
        try:
            if mode == "agentic":
                rc, stdout, stderr, hang_killed = _run_with_watchdog(
                    cmd, safe_prompt, timeout, cwd,
                    hang_grace, hang_window, hang_threshold,
                )
            else:
                # JSON mode: simple subprocess.run is fine.
                proc = subprocess.run(
                    cmd, input=safe_prompt, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd,
                )
                rc, stdout, stderr, hang_killed = (
                    proc.returncode, proc.stdout or "", proc.stderr or "", False,
                )

            output = stdout.strip()

            # ── Agentic mode ────────────────────────────────────────────────
            if mode == "agentic":
                # Hang or non-zero exit ⇒ failure
                if hang_killed or rc != 0:
                    log_path = _write_failure_log(
                        safe_executable, rc, stdout, stderr, safe_prompt
                    )
                    err = (stderr or "").strip()
                    if len(err) > 4000:
                        err = err[:2000] + "\n…[truncated, see " + str(log_path) + "]…\n" + err[-2000:]
                    out_tail = (output or "").strip()
                    if len(out_tail) > 1000:
                        out_tail = "…" + out_tail[-1000:]
                    label = "HUNG (CPU flat-line)" if hang_killed else f"exited {rc}"
                    return _error_response(
                        f"Agentic CLI {label}. log={log_path.name} "
                        f"stderr: {err}\nstdout-tail: {out_tail}"
                    )

                # WF-8: zero-diff classification (three-way, not binary)
                if expects_changes:
                    post_changed, post_stat = _git_diff_summary(cwd)
                    files_changed = post_changed and (post_stat != _pre_stat)
                    if not files_changed:
                        verdict = _classify_zero_diff_summary(output)
                        if verdict == 'verified_complete':
                            # WF-8 v3: verify the files Claude mentions ACTUALLY EXIST.
                            # Catches the L0130-GEO-1 pattern where Claude claimed
                            # "existing files satisfy the spec" but the files were
                            # missing entirely.
                            referenced = _extract_referenced_paths(prompt, output)
                            all_exist, exists, missing = _verify_referenced_files_exist(
                                referenced, cwd
                            )
                            if not all_exist:
                                log.warning(
                                    "FALSE-COMPLETE (hallucinated existence): Claude "
                                    "claims complete but %d/%d referenced files are "
                                    "missing: %s",
                                    len(missing), len(referenced),
                                    missing[:5],
                                )
                                log_path = _write_failure_log(
                                    safe_executable, rc, stdout, stderr, safe_prompt
                                )
                                return {
                                    "action": "need_help",
                                    "params": {
                                        "reason": (
                                            "Hallucinated 'verified-complete': "
                                            f"{len(missing)} of {len(referenced)} files "
                                            f"Claude referenced do not exist on disk. "
                                            f"Missing: {', '.join(missing[:8])}. "
                                            f"Summary: {output[-300:]}"
                                        ),
                                    },
                                    "reasoning": (
                                        f"WF-8 v3 file-existence check failed "
                                        f"(log={log_path.name})."
                                    ),
                                }
                            log.info(
                                "VERIFIED-COMPLETE: exit 0 with no changes; %d "
                                "referenced files exist on disk; summary asserts "
                                "criteria met",
                                len(exists),
                            )
                            return {
                                "action": "step_done",
                                "params": {
                                    "summary": (
                                        f"[verified-complete: {len(exists)} files "
                                        f"exist; existing implementation satisfies "
                                        f"the task] {output[-500:]}"
                                    ),
                                },
                                "reasoning": (
                                    "WF-8 v3 verified-complete: zero diff + "
                                    "completion phrases + all referenced files "
                                    "exist on disk."
                                ),
                            }
                        if verdict == 'blocked':
                            log.warning(
                                "FALSE-SUCCESS (blocked): exit 0 but no files changed "
                                "and summary indicates Claude cannot proceed"
                            )
                            log_path = _write_failure_log(
                                safe_executable, rc, stdout, stderr, safe_prompt
                            )
                            return {
                                "action": "need_help",
                                "params": {
                                    "reason": (
                                        "False-success (blocked): CLI exited 0 but no "
                                        "files were modified and summary indicates "
                                        f"blockage. Summary: {output[-400:]}"
                                    ),
                                },
                                "reasoning": (
                                    f"WF-8 false-success/blocked (log={log_path.name})."
                                ),
                            }
                        # verdict == 'unknown' → conservative default: need_help
                        log.warning(
                            "FALSE-SUCCESS (unclear): exit 0, no files changed, "
                            "summary is neutral — defaulting to need_help"
                        )
                        log_path = _write_failure_log(
                            safe_executable, rc, stdout, stderr, safe_prompt
                        )
                        return {
                            "action": "need_help",
                            "params": {
                                "reason": (
                                    "False-success (unclear): CLI exited 0 but no "
                                    "files were modified and the summary is neutral. "
                                    f"Summary: {output[-400:]}"
                                ),
                            },
                            "reasoning": (
                                f"WF-8 false-success/unclear (log={log_path.name})."
                            ),
                        }

                summary = output[-800:] if output else "Task completed (no output)."
                return {
                    "action": "step_done",
                    "params": {"summary": summary},
                    "reasoning": "Agentic CLI returned exit 0 with verified changes; treated as complete.",
                }

            # ── JSON-protocol mode ──────────────────────────────────────────
            if not output and stderr.strip():
                _write_failure_log(safe_executable, rc, stdout, stderr, safe_prompt)
                return _error_response(
                    f"CLI returned no stdout. stderr: {stderr[:500]}"
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
