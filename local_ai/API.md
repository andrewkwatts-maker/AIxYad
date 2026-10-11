# local_ai gateway API (v1)

The gateway is one local HTTP service in front of every model server on this PC and the
LAN. It routes requests by role, queues them per model server, swaps models that share
a GPU, runs plan-then-delegate jobs, and serves the status that the VS Code extension
and the phone page show.

Default address: `http://127.0.0.1:8770`. To reach it from a phone, use `tailscale serve`
or add a Tailscale address to `gateway.listen`.

## Auth

Every request needs `Authorization: Bearer <token>`. The token is in
`~/.aixyad/gateway.token` and is created on first start (43 url-safe characters).

There are two exceptions:

- `GET /ui?token=<token>` sets an `HttpOnly; SameSite=Strict` cookie and redirects to `/ui`.
  After that the cookie is accepted in place of the header.
- `GET /health` needs no token and returns `{"status":"ok"}`.

The gateway sends no CORS headers, so other web pages cannot call it from a browser.

## Model API (proxied)

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI style; `stream: true` passes SSE through |
| POST | `/v1/completions` | |
| POST | `/v1/messages` | Anthropic style (llama-server answers it natively) |
| POST | `/v1/messages/count_tokens` | |
| GET  | `/v1/models` | one entry per route and per backend alias |

The body's `model` field picks the destination:

- A **route** name (`planner`, `worker`, `fast`, `vision`, ...) maps to a backend plus
  request defaults. For example, `worker` turns thinking off and caps `max_tokens`.
- A backend's own model **alias** goes straight to that backend.
- Anything else goes to the `default` route.

Responses carry `X-AIxYad-Backend: <backend name>`.

## Status

`GET /gateway/status`

```json
{
  "version": 1,
  "backends": [
    {"name": "big", "url": "http://127.0.0.1:11435", "model": "qwen3.8-flash-next",
     "group": "gpu0", "managed": true, "state": "ready",
     "slots_total": 4, "slots_busy": 1, "queued": 2,
     "last_decode_tps": 15.6, "last_prefill_tps": 240.0, "profile": "big",
     "vars": {"ctx": "65536", "slots": "2", "cache": "q8_0"}, "error": null}
  ],
  "routes": {"planner": "big", "worker": "small-gpu", "fast": "lan-small", "vision": "lan-vision"},
  "jobs": {"running": 1, "queued": 0}
}
```

`state` is one of `stopped`, `starting`, `ready`, `draining` (finishing queued work
before a swap), `stopping`, `unreachable` (a remote backend that does not answer) or
`error`.

## Model servers (managed backends only)

| Method | Path | Body |
|---|---|---|
| GET  | `/gateway/profiles` | returns `[{"name", "description", "vars": {"ctx": "65536", ...}}]` |
| POST | `/gateway/backends/{name}/start` | `{"profile": "big", "vars": {"ctx": 131072, "slots": 2, "cache": "q4_0"}}` (both optional) |
| POST | `/gateway/backends/{name}/stop` | `{}` |

`vars` tunes a profile at load time. The `big` profile has these variables:

- `ctx`: context per agent, in tokens
- `slots`: how many agents are served at once
- `cache`: `q8_0` or `q4_0`
- `ub`: the batch size for reading prompts
- `fit_margin`: MiB of VRAM to leave free

A variable name the profile doesn't have is answered with 400. If the server is already
loaded with different settings, it restarts with the new ones. A server that is busy
answers 409.

## Jobs: plan, review, delegate

`POST /gateway/jobs`

```json
{"task": "Add retry with backoff to the HTTP adapter", "cwd": "H:/Projects/X",
 "mode": "plan_and_run", "cli": "claude", "review_plan": true,
 "planner": "planner", "worker": "worker", "max_parallel": 2,
 "worktrees": true, "allow_shell": false}
```

Field notes:

- `mode`:
  - `plan` stops after the plan.
  - `run` sends the whole task to one worker.
  - `plan_and_run` has the planner split the task into steps, then runs each step
    as a headless CLI agent on the worker route.
- `review_plan: true` holds the job in `awaiting_approval` until `/approve`.
- `cli`: `claude` (Claude Code, through `/v1/messages`) or `qwen` (Qwen Code, through
  `/v1/chat/completions`). Each runs with its own config folder.
- `worktrees: true` runs each step in its own git worktree under
  `<cwd>/.aixyad/worktrees/<job>-<step>`, so steps never edit the same files.

The reply is `{"id": "j_20261010_184501_ab12"}`.

`GET /gateway/jobs` returns `[JobSummary]`, newest first:

```json
{"id": "...", "title": "...", "status": "running", "mode": "plan_and_run",
 "created": "2026-10-10T18:45:01", "steps_total": 4, "steps_done": 1, "cwd": "..."}
```

`status` is one of `queued`, `planning`, `awaiting_approval`, `running`, `done`, `failed`
or `cancelled`.

`GET /gateway/jobs/{id}` returns a JobSummary plus:

```json
{"task": "...", "plan": [{"id": "s1", "title": "...", "instructions": "...",
  "files": ["a.py"], "depends_on": [], "status": "done", "worktree": "...",
  "summary": "..."}], "error": null}
```

A step's `status` is one of `pending`, `running`, `done`, `failed` or `cancelled`.

`GET /gateway/jobs/{id}/events?after=<seq>` returns events with `seq > after` (start with
`after=0`). It long-polls for up to 20 s while the job is active. For a finished job it
returns at once. `next` is the last seq returned (or `after` when there are none); pass it
back as `after`.

```json
{"events": [{"seq": 12, "ts": "2026-10-10T18:45:09", "step": "s1",
             "kind": "text", "text": "Reading adapter...", "data": null}],
 "next": 12, "status": "running"}
```

Event `kind` is one of:

- `status`: job or step status changed
- `plan`: the plan is ready; `data` holds it
- `text`: agent text
- `tool_use`: `data` = `{"name", "input"}`
- `tool_result`
- `stderr`
- `error`
- `result`: final summary of a step (for the job itself, `data` = `{"diff_stat", "worktree"}`)

A `plan` event's `data` is `{"summary": "...", "steps": [Step, ...]}`. Each Step has the
fields shown under `GET /gateway/jobs/{id}`.

| Method | Path | Body |
|---|---|---|
| POST | `/gateway/jobs/{id}/approve` | `{"plan": [...]}` (optional edited plan) → 200 `{"ok": true}` |
| POST | `/gateway/jobs/{id}/cancel` | kills the job's process trees → 200 `{"ok": true}` |

An edited plan step needs `instructions`. `id`, `title`, `files` and `depends_on` are
optional. A `depends_on` entry naming a removed step is dropped. `POST /gateway/jobs` replies
201 `{"id"}`. Start and stop reply 202 `{"ok": true}`; loading continues in the background, so
watch `state` in the status. `/v1/models` uses the OpenAI form `{"object":"list","data":[{"id",...}]}`.

## Errors

Errors are returned as JSON, `{"error": {"message": "...", "type": "..."}}`, with status
400, 401, 404, 409 (for example, a stop requested while work is queued) or 502 (the
backend is unreachable).
