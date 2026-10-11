"""Tests for local_ai: profiles, scheduler, proxy, gateway HTTP, CLI events and jobs.

No model is loaded: model servers are replaced by a small fake HTTP backend, process
control by a fake controller, and CLI agents by a fake runner.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from local_ai import cli_agents, jobs as jobs_mod
from local_ai.cli_agents import AgentEvent, claude_command, parse_line
from local_ai.gateway import COOKIE, Gateway, Handler, ensure_token
from local_ai.gateway_config import BackendSpec, GatewaySpec, JobsSpec, RouteSpec, _parallel_slots, load_gateway
from local_ai.jobs import JobManager, parse_plan
from local_ai.profiles import load_profiles
from local_ai.proxy import _timings_from_sse, prepare_body
from local_ai.scheduler import ERROR, READY, STOPPED, BackendUnavailable, Scheduler


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- profiles ------------------------------------------------------------------
def test_profiles_merge_user_file_and_extends(tmp_path, monkeypatch):
    user = tmp_path / "local_ai.yaml"
    user.write_text(
        "defaults:\n  binary: /bin/llama-server\n  model: /m/big.gguf\n"
        "profiles:\n  mine:\n    extends: big\n    vars: {slots: 3}\n    args: [-ub, 1024, --flash-attn, off]\n",
        encoding="utf-8")
    profiles = load_profiles(user)
    mine = profiles["mine"]
    assert mine.binary == "/bin/llama-server" and mine.model == "/m/big.gguf"
    assert mine.args[-4:] == ("-ub", "1024", "--flash-attn", "off")  # YAML `off` -> "off"
    assert "--fit" in mine.args  # inherited from big
    # --fit gives up when any tensor override is set, and every layer then lands on the GPU
    assert "--cpu-moe" not in mine.args and "-ot" not in mine.args
    assert mine.command()[:3] == ["/bin/llama-server", "-m", "/m/big.gguf"]
    assert _parallel_slots(mine.args) == 3  # child vars override the parent's


def test_gateway_config_resolves_backend_from_profile(tmp_path):
    user = tmp_path / "local_ai.yaml"
    user.write_text("defaults:\n  model: /m/big.gguf\n", encoding="utf-8")
    spec = load_gateway(user)
    big = spec.backends["big"]
    assert big.url == "http://127.0.0.1:11435" and big.model == "qwen3.8-flash-next" and big.slots == 2
    route, backend = spec.route_for("worker")
    assert route.thinking is False and backend is big
    assert spec.route_for("qwen3.8-flash-next") == (None, big)
    assert spec.route_for("claude-sonnet-whatever")[0].name == "planner"  # default route


# -- scheduler -----------------------------------------------------------------
class FakeController:
    def __init__(self, fail=()):
        self.events: list[str] = []
        self.fail = set(fail)
        self.release = threading.Event()
        self.release.set()

    def start(self, backend, profile, variables):
        self.release.wait(5)
        self.events.append(f"start {backend.name}" + (f" {variables}" if variables else ""))
        if backend.name in self.fail:
            raise RuntimeError("out of memory")

    def stop(self, backend):
        self.events.append(f"stop {backend.name}")


def managed(name, slots=1, group="gpu0"):
    return BackendSpec(name=name, url=f"http://x/{name}", model=name, slots=slots, group=group, profile=name)


def test_scheduler_starts_on_demand_and_limits_slots():
    ctl = FakeController()
    sched = Scheduler([managed("big", slots=2)], ctl)
    a = sched.acquire("big", 5)
    b = sched.acquire("big", 5)
    assert ctl.events == ["start big"]
    got = []
    t = threading.Thread(target=lambda: got.append(sched.acquire("big", 5)))
    t.start()
    time.sleep(0.2)
    assert not got and sched.status()[0]["queued"] == 1  # third waits for a slot
    a.release()
    t.join(5)
    assert got and sched.status()[0]["slots_busy"] == 2
    b.release()
    got[0].release()


def test_scheduler_drains_queue_before_swapping_models():
    ctl = FakeController()
    sched = Scheduler([managed("big"), managed("small")], ctl, max_hold_s=60)
    lease_big = sched.acquire("big", 5)
    order = []

    def use(name):
        with sched.acquire(name, 10):
            order.append(name)

    waiting_small = threading.Thread(target=use, args=("small",))
    waiting_small.start()
    time.sleep(0.1)
    more_big = threading.Thread(target=use, args=("big",))
    more_big.start()
    time.sleep(0.1)
    lease_big.release()
    waiting_small.join(5)
    more_big.join(5)
    # queued work for the loaded model runs before the GPU is handed over
    assert order == ["big", "small"]
    assert ctl.events == ["start big", "stop big", "start small"]


def test_scheduler_drains_when_other_model_waits_too_long():
    now = [0.0]
    ctl = FakeController()
    sched = Scheduler([managed("big"), managed("small")], ctl, max_hold_s=30, clock=lambda: now[0])
    hold = sched.acquire("big", 5)
    t = threading.Thread(target=lambda: sched.acquire("small", 60).release())
    t.start()
    time.sleep(0.1)
    now[0] = 31.0
    sched.tick()
    assert sched.status()[0]["state"] == "draining"
    hold.release()
    t.join(5)
    assert ctl.events[-2:] == ["stop big", "start small"]


def test_scheduler_reports_start_failure_without_retry_loop():
    ctl = FakeController(fail={"big"})
    sched = Scheduler([managed("big")], ctl)
    with pytest.raises(BackendUnavailable, match="out of memory"):
        sched.acquire("big", 5)
    assert ctl.events == ["start big"]
    assert sched.status()[0]["state"] == ERROR
    with pytest.raises(BackendUnavailable):
        sched.acquire("big", 1)
    assert ctl.events == ["start big"]  # no automatic restart


def test_scheduler_restarts_with_new_settings_only_when_idle():
    ctl = FakeController()
    sched = Scheduler([managed("big", slots=2)], ctl)
    lease = sched.acquire("big", 5)
    with pytest.raises(RuntimeError, match="busy"):
        sched.start("big", "big", {"ctx": "131072"}, slots=1)
    assert sched.status()[0]["vars"] == {}  # refused: nothing changed
    lease.release()
    sched.start("big", "big", {"ctx": "131072"}, slots=1)
    assert wait_for(lambda: sched.status()[0]["state"] == READY and len(ctl.events) == 3)
    assert ctl.events[1:] == ["stop big", "start big {'ctx': '131072'}"]
    status = sched.status()[0]
    assert status["vars"] == {"ctx": "131072"} and status["slots_total"] == 1
    sched.start("big", "big", {"ctx": "131072"})  # same settings: no restart
    assert len(ctl.events) == 3


def test_profile_vars_fill_args_and_reject_unknown(tmp_path):
    user = tmp_path / "local_ai.yaml"
    user.write_text("defaults:\n  model: /m/big.gguf\n", encoding="utf-8")
    big = load_profiles(user)["big"]
    tuned = big.with_vars({"ctx": 131072, "slots": 1, "cache": "q4_0"})
    args = list(tuned.args)
    assert args[args.index("-c") + 1] == "131072" and args[args.index("--fit-ctx") + 1] == "131072"
    assert args[args.index("-ctk") + 1] == "q4_0" and _parallel_slots(tuned.args) == 1
    assert "{" not in " ".join(big.args)
    with pytest.raises(ValueError, match="no variables"):
        big.with_vars({"context": 5})


def test_gateway_start_with_vars_over_http(tmp_path):
    user = tmp_path / "local_ai.yaml"
    user.write_text(
        "defaults:\n  model: /m/big.gguf\n"
        f"gateway:\n  token_file: {(tmp_path / 'tok').as_posix()}\n"
        f"  jobs:\n    state_dir: {(tmp_path / 'jobs').as_posix()}\n", encoding="utf-8")
    ctl = FakeController()
    gw = Gateway(load_gateway(user), controller=ctl)
    handler = type("H2", (Handler,), {"gateway": gw})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        code, _, _ = call(base, "/gateway/backends/big/start", gw.token, {"vars": {"ctx": 98304, "slots": 1}})
        assert code == 202
        assert wait_for(lambda: gw.scheduler.status()[0]["state"] == READY)
        assert ctl.events == ["start big {'ctx': '98304', 'slots': '1'}"]
        assert gw.scheduler.status()[0]["slots_total"] == 1
        assert call(base, "/gateway/backends/big/start", gw.token, {"vars": {"nope": 1}})[0] == 400
        profiles = json.loads(call(base, "/gateway/profiles", gw.token)[2])
        assert next(p for p in profiles if p["name"] == "big")["vars"]["ctx"] == "65536"
    finally:
        server.shutdown()


def test_scheduler_idle_unload_and_unreachable_remote():
    now = [0.0]
    ctl = FakeController()
    remote = BackendSpec(name="lan", url="http://lan", model="small", slots=1)
    sched = Scheduler([managed("big"), remote], ctl, idle_unload_s=10, clock=lambda: now[0])
    sched.acquire("big", 5).release()
    now[0] = 11
    sched.tick()
    assert wait_for(lambda: sched.status()[0]["state"] == STOPPED)
    sched.set_reachable("lan", False, "timed out")
    with pytest.raises(BackendUnavailable, match="unreachable"):
        sched.acquire("lan", 1)
    sched.set_reachable("lan", True)
    sched.acquire("lan", 1).release()
    assert sched.status()[1]["state"] == READY


# -- proxy ---------------------------------------------------------------------
def test_prepare_body_applies_route_defaults():
    backend = BackendSpec(name="big", url="http://x", model="qwen-big")
    route = RouteSpec(name="worker", backend="big", thinking=False, max_tokens=1000, params={"temperature": 0.2})
    body = prepare_body({"model": "worker", "max_tokens": 5000, "thinking": {"type": "enabled"},
                         "chat_template_kwargs": {"x": 1}}, route, backend)
    assert body["model"] == "qwen-big" and body["max_tokens"] == 1000
    assert body["chat_template_kwargs"] == {"x": 1, "enable_thinking": False}
    assert "thinking" not in body and body["temperature"] == 0.2
    assert prepare_body({"model": "x", "temperature": 1}, route, backend)["temperature"] == 1


def test_timings_from_sse_reads_last_chunk():
    stream = 'data: {"choices":[]}\n\ndata: {"choices":[],"timings":{"predicted_per_second":12.5}}\n\ndata: [DONE]\n'
    assert _timings_from_sse(stream) == {"predicted_per_second": 12.5}


# -- gateway over HTTP -----------------------------------------------------------
class FakeBackend(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeBackend.seen.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                                 "x-api-key": self.headers.get("x-api-key")})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part in ('data: {"delta":"he"}\n\n', 'data: {"delta":"llo","timings":{"predicted_n":20,"predicted_per_second":9.5}}\n\n'):
                self.wfile.write(part.encode())
                self.wfile.flush()
            return
        payload = json.dumps({"ok": True, "model": body["model"],
                              "timings": {"predicted_n": 50, "predicted_per_second": 11.0,
                                          "prompt_n": 100, "prompt_per_second": 300.0}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def gateway(tmp_path):
    FakeBackend.seen = []
    backend_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
    threading.Thread(target=backend_server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{backend_server.server_address[1]}"
    backend = BackendSpec(name="lan", url=url, model="small-model", slots=1, api_key="backend-key")
    spec = GatewaySpec(
        listen=(("127.0.0.1", 0),), token_file=tmp_path / "token", default_route="worker",
        backends={"lan": backend},
        routes={"worker": RouteSpec(name="worker", backend="lan", thinking=False)},
        profiles={}, jobs=JobsSpec(state_dir=tmp_path / "jobs", cli_config_root=tmp_path / "cli"))
    gw = Gateway(spec, controller=FakeController())
    handler = type("TestHandler", (Handler,), {"gateway": gw})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, gw
    server.shutdown()
    backend_server.shutdown()


def call(base, path, token=None, body=None, headers=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, dict(reply.headers), reply.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_gateway_requires_token(gateway):
    base, gw = gateway
    assert call(base, "/health")[0] == 200
    assert call(base, "/gateway/status")[0] == 401
    assert call(base, "/gateway/status", token="wrong")[0] == 401
    code, _, body = call(base, "/gateway/status", token=gw.token)
    status = json.loads(body)
    assert code == 200 and status["backends"][0]["name"] == "lan" and status["routes"] == {"worker": "lan"}


def test_gateway_favicon_served_without_auth(gateway):
    base, gw = gateway
    code, headers, body = call(base, "/favicon.svg")
    assert code == 200
    assert headers.get("Content-Type") == "image/svg+xml"
    assert b"<svg" in body


def test_gateway_routes_and_never_forwards_its_token(gateway):
    base, gw = gateway
    code, headers, body = call(base, "/v1/chat/completions", body={"model": "worker", "messages": []},
                               headers={"x-api-key": gw.token})
    assert code == 200 and json.loads(body)["model"] == "small-model"
    assert headers["X-AIxYad-Backend"] == "lan"
    seen = FakeBackend.seen[-1]
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["auth"] == "Bearer backend-key" and seen["x-api-key"] is None
    status = json.loads(call(base, "/gateway/status", token=gw.token)[2])
    assert status["backends"][0]["last_decode_tps"] == 11.0
    assert status["backends"][0]["last_prefill_tps"] == 300.0


def test_gateway_streams_sse(gateway):
    base, gw = gateway
    code, headers, body = call(base, "/v1/chat/completions", token=gw.token,
                               body={"model": "worker", "stream": True, "messages": []})
    assert code == 200 and b'"delta":"he"' in body and b'"delta":"llo"' in body
    assert json.loads(call(base, "/gateway/status", token=gw.token)[2])["backends"][0]["last_decode_tps"] == 9.5


def test_ui_token_login_sets_cookie(gateway):
    base, gw = gateway
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    request = urllib.request.Request(f"{base}/ui?token={gw.token}")
    with pytest.raises(urllib.error.HTTPError):  # wrong token
        urllib.request.urlopen(f"{base}/ui?token=nope", timeout=5)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(request, timeout=5)
    assert caught.value.code == 303
    cookie = caught.value.headers["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE}={gw.token}") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    code, _, page = call(base, "/ui", headers={"Cookie": f"{COOKIE}={gw.token}"})
    assert code == 200 and "<title>AI × יד</title>".encode("utf-8") in page


def test_ensure_token_is_stable(tmp_path):
    first = ensure_token(tmp_path / "t")
    assert len(first) >= 40 and ensure_token(tmp_path / "t") == first


# -- CLI agents --------------------------------------------------------------------
def test_parse_claude_stream_json():
    assert parse_line('{"type":"system","subtype":"init","model":"worker"}')[0].text == "agent started (worker)"
    events = parse_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading"}, {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}))
    assert [e.kind for e in events] == ["text", "tool_use"] and events[1].data["input"] == {"file_path": "a.py"}
    result = parse_line('{"type":"result","result":"done","is_error":false,"num_turns":3}')[0]
    assert result.kind == "result" and result.text == "done" and result.data["num_turns"] == 3
    assert parse_line("plain text")[0].kind == "text"


def test_claude_command_isolates_config_and_drops_real_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    cmd = claude_command(binary="claude", gateway_url="http://127.0.0.1:8770", token="tok", route="worker",
                         planner_route="planner", worker_route="worker", config_dir=tmp_path / "c",
                         read_only=False, allow_shell=False)
    assert "ANTHROPIC_API_KEY" not in cmd.env
    assert cmd.env["ANTHROPIC_AUTH_TOKEN"] == "tok" and cmd.env["ANTHROPIC_MODEL"] == "worker"
    assert cmd.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "c")
    assert cmd.argv[cmd.argv.index("--disallowedTools") + 1] == "Bash"
    assert "Edit" in cmd.argv[cmd.argv.index("--allowedTools") + 1]
    planner = claude_command(binary="claude", gateway_url="u", token="t", route="planner", planner_route="planner",
                             worker_route="worker", config_dir=tmp_path / "c", read_only=True, allow_shell=True)
    assert "Edit" not in planner.argv[planner.argv.index("--allowedTools") + 1]


# -- jobs ----------------------------------------------------------------------------
def test_parse_plan_from_messy_reply():
    reply = 'Here is the plan:\n```json\n{"summary": "s", "steps": [{"id": "a", "title": "A", "instructions": "do a", ' \
            '"files": ["x.py"]}, {"id": "b", "instructions": "do b", "depends_on": ["a", "zzz"]}]}\n```'
    summary, steps = parse_plan(reply, 8)
    assert summary == "s" and [s.id for s in steps] == ["a", "b"] and steps[1].depends_on == ["a"]
    with pytest.raises(ValueError):
        parse_plan("no plan here", 8)


class FakeRunner:
    def __init__(self, plan):
        self.plan = plan
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def run(self, command, cwd, prompt, on_event, cancel, timeout_s):
        route = command.env.get("ANTHROPIC_MODEL")
        with self.lock:
            self.calls.append((route, cwd))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if route == "planner":
                on_event(AgentEvent("result", json.dumps(self.plan)))
                return 0, json.dumps(self.plan)
            time.sleep(0.2)
            step_id = prompt.split("Your step (", 1)[1].split(")", 1)[0]
            Path(cwd, f"{step_id}.txt").write_text("changed", encoding="utf-8")
            on_event(AgentEvent("result", f"{step_id} finished"))
            return 0, f"{step_id} finished"
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True)
    return root


def make_manager(tmp_path, runner):
    spec = JobsSpec(state_dir=tmp_path / "state", cli_config_root=tmp_path / "cli",
                    worktree_root=str(tmp_path / "wt" / "{repo_name}"))
    return JobManager(spec, "http://127.0.0.1:8770", "tok", runner=runner)


def test_job_plans_waits_for_approval_and_runs_steps_in_a_worktree(tmp_path, repo):
    plan = {"summary": "two parts", "steps": [
        {"id": "s1", "title": "one", "instructions": "edit x", "files": ["x.py"]},
        {"id": "s2", "title": "two", "instructions": "edit y", "files": ["y.py"]},
        {"id": "s3", "title": "three", "instructions": "edit x again", "files": ["x.py"], "depends_on": ["s1"]}]}
    runner = FakeRunner(plan)
    manager = make_manager(tmp_path, runner)
    job_id = manager.create({"task": "do things", "cwd": str(repo), "max_parallel": 2})
    assert wait_for(lambda: manager.get(job_id)["status"] == "awaiting_approval")
    assert runner.calls == [("planner", str(repo))]
    manager.approve(job_id)
    assert wait_for(lambda: manager.get(job_id)["status"] == "done", 15), manager.get(job_id)
    job = manager.get(job_id)
    worktree = Path(job["worktree"])
    assert worktree.parent == tmp_path / "wt" / "proj"
    assert {p.name for p in worktree.glob("s*.txt")} == {"s1.txt", "s2.txt", "s3.txt"}
    assert not list(repo.glob("s*.txt"))  # the user's working copy is untouched
    assert runner.max_active == 2  # s1 and s2 together; s3 waited for s1 (same file)
    branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == f"claude/local-ai/{job_id}"
    kinds = [e["kind"] for e in manager.events(job_id, 0, wait_s=0)["events"]]
    assert "plan" in kinds and kinds.count("result") >= 4


def test_job_approval_with_edited_plan_and_cancel(tmp_path, repo):
    runner = FakeRunner({"steps": [{"id": "s1", "instructions": "x"}]})
    manager = make_manager(tmp_path, runner)
    job_id = manager.create({"task": "t", "cwd": str(repo), "worktrees": False})
    assert wait_for(lambda: manager.get(job_id)["status"] == "awaiting_approval")
    manager.approve(job_id, [{"id": "e1", "title": "edited", "instructions": "only this"}])
    assert wait_for(lambda: manager.get(job_id)["status"] == "done", 10)
    assert [s["id"] for s in manager.get(job_id)["plan"]] == ["e1"]
    assert (repo / "e1.txt").exists()  # worktrees off: works in place

    second = manager.create({"task": "t2", "cwd": str(repo)})
    assert wait_for(lambda: manager.get(second)["status"] == "awaiting_approval")
    manager.cancel(second)
    assert wait_for(lambda: manager.get(second)["status"] == "cancelled")


def test_job_rejects_bad_requests(tmp_path):
    manager = make_manager(tmp_path, FakeRunner({}))
    with pytest.raises(ValueError):
        manager.create({"task": "", "cwd": str(tmp_path)})
    with pytest.raises(ValueError):
        manager.create({"task": "x", "cwd": str(tmp_path / "missing")})
    with pytest.raises(ValueError):
        manager.create({"task": "x", "cwd": str(tmp_path), "mode": "bogus"})


def test_request_log_redacts_token():
    from local_ai.gateway import _TOKEN_IN_URL
    line = '"GET /ui?token=abc-DEF_123 HTTP/1.1" 303 -'
    assert _TOKEN_IN_URL.sub(r"\1<redacted>", line) == '"GET /ui?token=<redacted> HTTP/1.1" 303 -'
    assert _TOKEN_IN_URL.sub(r"\1<redacted>", "/x?a=1&token=zz") == "/x?a=1&token=<redacted>"


def test_event_cursor_never_skips(tmp_path, repo):
    manager = make_manager(tmp_path, FakeRunner({"steps": [{"id": "s1", "instructions": "x"}]}))
    job_id = manager.create({"task": "t", "cwd": str(repo), "mode": "plan"})
    assert wait_for(lambda: manager.get(job_id)["status"] == "done")
    seen, after = [], 0
    for _ in range(10):
        page = manager.events(job_id, after, wait_s=0)
        seen += [e["seq"] for e in page["events"]]
        if not page["events"]:
            break
        after = page["next"]
    assert seen == list(range(1, len(seen) + 1)) and len(seen) >= 3  # contiguous, nothing skipped
