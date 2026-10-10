"""Jobs: a planner agent splits a task into steps, worker agents carry them out.

Flow for `plan_and_run`: the planner (read-only tools, `planner` route, usually the big
model) explores the repository and returns a JSON plan. With `review_plan` the job waits
for approval, during which the plan may be edited. The steps then run as separate
headless CLI agents on the `worker` route, in one git worktree per job, so the user's
working copy is untouched. Steps run in parallel when their dependencies are done and
their file lists do not overlap. Nothing is committed: the worktree holds the changes
for the user to review and merge.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from adapters._base import _extract_json
from adapters.subprocess_cli import _kill_tree
from local_ai.cli_agents import AgentCommand, AgentEvent, claude_command, parse_line, qwen_command
from local_ai.gateway_config import JobsSpec

ACTIVE = ("queued", "planning", "awaiting_approval", "running")

PLAN_PROMPT = """You are the planner for a team of coding agents working in this repository.

Task: {task}

Explore the code with your read-only tools as much as you need, then reply with ONLY a JSON object:
{{"summary": "<one paragraph>", "steps": [{{"id": "s1", "title": "<short>", "instructions": "<complete instructions>", "files": ["<path from repo root>"], "depends_on": []}}]}}

Rules:
- At most {max_steps} steps; fewer is better, and one step is right for a small task.
- Each step is done by a separate agent that sees only its own instructions and the code, so write complete, specific instructions: what to change, where, and how to check it.
- "files" lists the files the step will edit. Steps that edit the same file must be ordered with "depends_on".
- Do not edit anything yourself."""

STEP_PROMPT = """You are one of several coding agents carrying out a plan in this repository.

Overall task: {task}

Plan:
{outline}

Your step ({step_id}): {title}
{instructions}

Files you are expected to edit: {files}

Rules: do only your step; other agents handle the rest. Do not commit, push or create branches. Keep changes minimal and in the style of the surrounding code. When finished, reply with a short summary of what you changed."""


@dataclass
class Step:
    id: str
    title: str
    instructions: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    summary: str = ""


@dataclass
class Job:
    id: str
    task: str
    cwd: str
    mode: str = "plan_and_run"
    cli: str = "claude"
    review_plan: bool = True
    planner: str = "planner"
    worker: str = "worker"
    max_parallel: int = 2
    worktrees: bool = True
    allow_shell: bool = False
    status: str = "queued"
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    title: str = ""
    plan: list[Step] = field(default_factory=list)
    plan_summary: str = ""
    worktree: str | None = None
    error: str | None = None
    events: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status, "mode": self.mode,
                "created": self.created, "cwd": self.cwd, "steps_total": len(self.plan),
                "steps_done": sum(s.status == "done" for s in self.plan)}

    def detail(self) -> dict:
        return {**self.summary(), "task": self.task, "plan": [asdict(s) for s in self.plan],
                "plan_summary": self.plan_summary, "worktree": self.worktree, "error": self.error,
                "cli": self.cli, "worktrees": self.worktrees, "allow_shell": self.allow_shell}


def parse_plan(text: str, max_steps: int) -> tuple[str, list[Step]]:
    """The planner's reply as (summary, steps); raises ValueError when it holds no plan."""
    data = _extract_json(text or "")
    if not data or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError("planner reply had no JSON plan with steps")
    steps: list[Step] = []
    for index, raw in enumerate(data["steps"][:max_steps], start=1):
        if not isinstance(raw, dict) or not str(raw.get("instructions", "")).strip():
            continue
        steps.append(Step(
            id=str(raw.get("id") or f"s{index}"),
            title=str(raw.get("title") or f"Step {index}")[:120],
            instructions=str(raw["instructions"]).strip(),
            files=[str(f) for f in raw.get("files") or [] if f],
            depends_on=[str(d) for d in raw.get("depends_on") or []],
        ))
    known = {s.id for s in steps}
    for step in steps:
        step.depends_on = [d for d in step.depends_on if d in known and d != step.id]
    if not steps:
        raise ValueError("planner's plan had no usable steps")
    return str(data.get("summary", "")), steps


class AgentRunner:
    """Runs one CLI agent process; separated so tests can substitute a fake."""

    def run(self, command: AgentCommand, cwd: str, prompt: str,
            on_event: Callable[[AgentEvent], None], cancel: threading.Event,
            timeout_s: float) -> tuple[int, str]:
        process = subprocess.Popen(
            command.argv, cwd=cwd, env=command.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt)
        process.stdin.close()
        deadline = time.monotonic() + timeout_s

        def watchdog() -> None:
            while process.poll() is None:
                if cancel.is_set() or time.monotonic() > deadline:
                    _kill_tree(process.pid)
                    return
                time.sleep(0.5)

        stderr_tail: list[str] = []

        def drain_stderr() -> None:
            for line in process.stderr:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-20]

        threading.Thread(target=watchdog, daemon=True).start()
        threading.Thread(target=drain_stderr, daemon=True).start()
        final = ""
        for line in process.stdout:
            for event in parse_line(line):
                on_event(event)
                if event.kind in ("result", "error"):
                    final = event.text
        code = process.wait()
        if code != 0 and stderr_tail:
            on_event(AgentEvent("stderr", "\n".join(stderr_tail[-10:])))
        if time.monotonic() > deadline:
            on_event(AgentEvent("error", f"stopped after {timeout_s:.0f}s"))
        return code, final


class JobManager:
    def __init__(self, spec: JobsSpec, gateway_url: str, token: str,
                 runner: AgentRunner | None = None) -> None:
        self._spec = spec
        self._gateway_url = gateway_url
        self._token = token
        self._runner = runner or AgentRunner()
        self._jobs: dict[str, Job] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._approved: dict[str, threading.Event] = {}
        self._cond = threading.Condition()
        spec.state_dir.mkdir(parents=True, exist_ok=True)
        self._load_saved()

    # -- public API ----------------------------------------------------------
    def create(self, request: dict[str, Any]) -> str:
        task = str(request.get("task", "")).strip()
        cwd = str(request.get("cwd", "")).strip()
        if not task:
            raise ValueError("task is required")
        if not cwd or not Path(cwd).is_dir():
            raise ValueError(f"cwd is not a folder: {cwd!r}")
        mode = request.get("mode", "plan_and_run")
        if mode not in ("plan", "run", "plan_and_run"):
            raise ValueError(f"unknown mode {mode!r}")
        cli = request.get("cli", "claude")
        if cli not in ("claude", "qwen"):
            raise ValueError(f"unknown cli {cli!r}")
        job = Job(
            id=f"j_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}", task=task, cwd=cwd,
            mode=mode, cli=cli, review_plan=bool(request.get("review_plan", True)),
            planner=str(request.get("planner", "planner")), worker=str(request.get("worker", "worker")),
            max_parallel=max(1, int(request.get("max_parallel", 2))),
            worktrees=bool(request.get("worktrees", True)),
            allow_shell=bool(request.get("allow_shell", False)),
            title=str(request.get("title") or task.splitlines()[0])[:80],
        )
        with self._cond:
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._approved[job.id] = threading.Event()
        self._save(job)
        threading.Thread(target=self._run_job, args=(job,), daemon=True, name=job.id).start()
        return job.id

    def list(self) -> list[dict]:
        with self._cond:
            return [j.summary() for j in sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)]

    def get(self, job_id: str) -> dict:
        with self._cond:
            return self._job(job_id).detail()

    def events(self, job_id: str, after: int, wait_s: float = 20.0) -> dict:
        deadline = time.monotonic() + wait_s
        with self._cond:
            job = self._job(job_id)
            while True:
                fresh = [e for e in job.events if e["seq"] > after]
                remaining = deadline - time.monotonic()
                if fresh or job.status not in ACTIVE or remaining <= 0:
                    # `next` is the last seq returned: pass it back as `after` (exclusive)
                    nxt = fresh[-1]["seq"] if fresh else after
                    return {"events": fresh, "next": nxt, "status": job.status}
                self._cond.wait(timeout=remaining)

    def approve(self, job_id: str, plan: list[dict] | None = None) -> None:
        with self._cond:
            job = self._job(job_id)
            if job.status != "awaiting_approval":
                raise RuntimeError(f"job is {job.status}, not awaiting approval")
            if plan is not None:
                _, job.plan = parse_plan(json.dumps({"steps": plan}), self._spec.max_plan_steps)
        self._approved[job_id].set()

    def cancel(self, job_id: str) -> None:
        with self._cond:
            job = self._job(job_id)
            if job.status not in ACTIVE:
                return
        self._cancel[job_id].set()
        self._approved[job_id].set()  # wake a job waiting for approval so it can stop

    def counts(self) -> dict[str, int]:
        with self._cond:
            statuses = [j.status for j in self._jobs.values()]
        return {"running": sum(s in ("planning", "running") for s in statuses),
                "queued": sum(s in ("queued", "awaiting_approval") for s in statuses)}

    # -- job flow ------------------------------------------------------------
    def _run_job(self, job: Job) -> None:
        cancel = self._cancel[job.id]
        try:
            if job.mode == "run":
                job.plan = [Step("s1", job.title, job.task)]
            else:
                self._set_status(job, "planning")
                self._plan(job, cancel)
                if job.mode == "plan":
                    self._set_status(job, "done")
                    return
                if job.review_plan:
                    self._set_status(job, "awaiting_approval")
                    self._approved[job.id].wait()
            if cancel.is_set():
                self._set_status(job, "cancelled")
                return
            workdir = self._prepare_workdir(job)
            self._set_status(job, "running")
            self._run_steps(job, workdir, cancel)
            self._finish(job, workdir, cancel)
        except Exception as exc:  # any failure ends the job with its reason
            job.error = str(exc)
            self._emit(job, None, "error", str(exc))
            self._set_status(job, "cancelled" if cancel.is_set() else "failed")

    def _plan(self, job: Job, cancel: threading.Event) -> None:
        prompt = PLAN_PROMPT.format(task=job.task, max_steps=self._spec.max_plan_steps)
        code, final = self._agent(job, None, job.planner, job.cwd, prompt, read_only=True, cancel=cancel)
        if cancel.is_set():
            raise RuntimeError("cancelled while planning")
        job.plan_summary, job.plan = parse_plan(final, self._spec.max_plan_steps)
        self._emit(job, None, "plan", job.plan_summary,
                   {"summary": job.plan_summary, "steps": [asdict(s) for s in job.plan]})
        self._save(job)

    def _prepare_workdir(self, job: Job) -> str:
        if not job.worktrees:
            return job.cwd
        top = self._git(job.cwd, "rev-parse", "--show-toplevel")
        if top is None:
            self._emit(job, None, "status", "not a git repository: working in place")
            return job.cwd
        root = Path(top)
        base = Path(self._spec.worktree_root.format(repo_parent=root.parent, repo_name=root.name))
        path = base / job.id
        base.mkdir(parents=True, exist_ok=True)
        branch = self._spec.branch_prefix + job.id
        if self._git(str(root), "worktree", "add", "-b", branch, str(path), "HEAD") is None:
            raise RuntimeError(f"could not create worktree {path}")
        job.worktree = str(path)
        self._emit(job, None, "status", f"worktree {path} on branch {branch}")
        relative = Path(job.cwd).resolve().relative_to(root.resolve()) if Path(job.cwd).resolve() != root.resolve() else Path()
        return str(path / relative)

    def _run_steps(self, job: Job, workdir: str, cancel: threading.Event) -> None:
        outline = "\n".join(f"  {s.id}. {s.title}" for s in job.plan)
        running: dict[str, threading.Thread] = {}

        def ready(step: Step) -> bool:
            if step.status != "pending":
                return False
            done = {s.id for s in job.plan if s.status == "done"}
            if any(d not in done for d in step.depends_on):
                return False
            busy_files = {f for s in job.plan if s.status == "running" for f in s.files}
            return not busy_files.intersection(step.files)

        def work(step: Step) -> None:
            prompt = STEP_PROMPT.format(task=job.task, outline=outline, step_id=step.id, title=step.title,
                                        instructions=step.instructions, files=", ".join(step.files) or "(not listed)")
            code, final = self._agent(job, step.id, job.worker, workdir, prompt, read_only=False, cancel=cancel)
            step.summary = final[:2000]
            self._set_step(job, step, "done" if code == 0 and not cancel.is_set() else
                           "cancelled" if cancel.is_set() else "failed")

        while True:
            with self._cond:
                for step_id in [k for k, t in running.items() if not t.is_alive()]:
                    running.pop(step_id)
                blocked = any(s.status == "failed" for s in job.plan)
                if cancel.is_set() or blocked:
                    for step in job.plan:
                        if step.status == "pending":
                            step.status = "cancelled"
                if not running and not any(s.status == "pending" for s in job.plan):
                    return
                for step in job.plan:
                    if len(running) >= job.max_parallel or cancel.is_set():
                        break
                    if ready(step):
                        step.status = "running"
                        thread = threading.Thread(target=work, args=(step,), daemon=True)
                        running[step.id] = thread
                        thread.start()
                if not running and any(s.status == "pending" for s in job.plan):
                    raise RuntimeError("remaining steps depend on steps that did not finish")
            time.sleep(0.5)

    def _finish(self, job: Job, workdir: str, cancel: threading.Event) -> None:
        changed = self._git(workdir, "status", "--short") or ""
        stat = self._git(workdir, "diff", "--stat") or ""
        self._emit(job, None, "result", "changes:\n" + (changed or "(none)"),
                   {"diff_stat": stat, "worktree": job.worktree})
        failed = any(s.status == "failed" for s in job.plan)
        self._set_status(job, "cancelled" if cancel.is_set() else "failed" if failed else "done")

    # -- helpers -------------------------------------------------------------
    def _agent(self, job: Job, step_id: str | None, route: str, cwd: str, prompt: str,
               *, read_only: bool, cancel: threading.Event) -> tuple[int, str]:
        config_dir = self._spec.cli_config_root / job.cli
        if job.cli == "claude":
            command = claude_command(
                binary=self._spec.claude_bin, gateway_url=self._gateway_url, token=self._token,
                route=route, planner_route=job.planner, worker_route=job.worker,
                config_dir=config_dir, read_only=read_only, allow_shell=job.allow_shell)
        else:
            command = qwen_command(
                binary=self._spec.qwen_bin, gateway_url=self._gateway_url, token=self._token,
                route=route, config_dir=config_dir, read_only=read_only, allow_shell=job.allow_shell)
        return self._runner.run(
            command, cwd, prompt,
            lambda event: self._emit(job, step_id, event.kind, event.text, event.data),
            cancel, self._spec.step_timeout_s)

    @staticmethod
    def _git(cwd: str, *args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def _job(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise KeyError(job_id)
        return self._jobs[job_id]

    def _emit(self, job: Job, step: str | None, kind: str, text: str, data: Any = None) -> None:
        with self._cond:
            seq = job.events[-1]["seq"] + 1 if job.events else 1
            job.events.append({"seq": seq, "ts": datetime.now().isoformat(timespec="seconds"),
                               "step": step, "kind": kind, "text": text, "data": data})
            self._cond.notify_all()

    def _set_status(self, job: Job, status: str) -> None:
        with self._cond:
            job.status = status
        self._emit(job, None, "status", status)
        self._save(job)

    def _set_step(self, job: Job, step: Step, status: str) -> None:
        with self._cond:
            step.status = status
        self._emit(job, step.id, "status", status)
        self._save(job)

    def _save(self, job: Job) -> None:
        with self._cond:
            record = {**job.detail(), "events": job.events[-500:]}
        path = self._spec.state_dir / f"{job.id}.json"
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")

    def _load_saved(self) -> None:
        for path in sorted(self._spec.state_dir.glob("j_*.json"))[-200:]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                plan = [Step(**{k: v for k, v in s.items() if k in Step.__dataclass_fields__})
                        for s in record.get("plan", [])]
                fields = {k: v for k, v in record.items() if k in Job.__dataclass_fields__ and k != "plan"}
                job = Job(**fields, plan=plan)
            except (OSError, ValueError, TypeError):
                continue
            if job.status in ACTIVE:
                job.status, job.error = "failed", "gateway restarted while the job was active"
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._approved[job.id] = threading.Event()
