"""Admission, queueing and GPU-sharing for model servers.

Each backend admits at most `slots` requests at once; the rest wait in FIFO order.
Managed backends in the same `group` share one GPU, so only one of them is loaded:

* A loaded backend keeps serving while it has work, including work that arrives while
  others wait, so queued requests for one model are batched before it is unloaded.
* When it has nothing queued or running and another member has waiting requests, the
  scheduler stops it and starts that member.
* If another member's oldest request has waited longer than `max_hold_s`, the loaded
  backend is put in `draining`: it stops admitting, finishes what is running, then swaps.

Starting and stopping servers is delegated to a controller, so the policy is testable
without launching llama-server.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

from local_ai.gateway_config import BackendSpec

STOPPED, STARTING, READY, DRAINING, STOPPING = "stopped", "starting", "ready", "draining", "stopping"
UNREACHABLE, ERROR = "unreachable", "error"
_LOADED = (STARTING, READY, DRAINING, STOPPING)


class BackendUnavailable(RuntimeError):
    """The backend failed to start, is unreachable, or the request waited too long."""


class Controller(Protocol):
    def start(self, backend: BackendSpec, profile: str, variables: dict[str, str]) -> None: ...
    def stop(self, backend: BackendSpec) -> None: ...


@dataclass
class _Waiter:
    enqueued: float


@dataclass
class _Backend:
    spec: BackendSpec
    state: str
    profile: str | None
    slots: int
    variables: dict[str, str] = field(default_factory=dict)
    busy: int = 0
    waiting: deque[_Waiter] = field(default_factory=deque)
    last_used: float = 0.0
    last_decode_tps: float | None = None
    last_prefill_tps: float | None = None
    error: str | None = None


@dataclass
class Lease:
    backend: BackendSpec
    _scheduler: "Scheduler"
    _released: bool = False

    def report(self, timings: dict | None) -> None:
        self._scheduler._record(self.backend.name, timings)

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._scheduler._release(self.backend.name)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class Scheduler:
    def __init__(
        self,
        backends: list[BackendSpec],
        controller: Controller,
        *,
        max_hold_s: float = 120.0,
        idle_unload_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._max_hold_s = max_hold_s
        self._idle_unload_s = idle_unload_s
        self._clock = clock
        self._cond = threading.Condition()
        self._backends = {
            b.name: _Backend(b, STOPPED if b.managed else READY, b.profile, b.slots) for b in backends
        }

    # -- public API ----------------------------------------------------------
    def acquire(self, name: str, timeout_s: float | None = None) -> Lease:
        """Block until `name` admits one more request; returns a lease to release."""
        deadline = None if timeout_s is None else self._clock() + timeout_s
        with self._cond:
            backend = self._backends[name]
            waiter = _Waiter(self._clock())
            backend.waiting.append(waiter)
            try:
                while True:
                    self._decide()
                    if backend.state in (ERROR, UNREACHABLE):
                        raise BackendUnavailable(f"{name} is {backend.state}: {backend.error or ''}".strip())
                    free = backend.slots - backend.busy
                    if backend.state == READY and free > 0 and waiter in list(backend.waiting)[:free]:
                        backend.waiting.remove(waiter)
                        backend.busy += 1
                        backend.last_used = self._clock()
                        return Lease(backend.spec, self)
                    remaining = None if deadline is None else deadline - self._clock()
                    if remaining is not None and remaining <= 0:
                        raise BackendUnavailable(f"{name}: queued longer than {timeout_s:.0f}s")
                    self._cond.wait(timeout=1.0 if remaining is None else min(1.0, remaining))
            except BaseException:
                if waiter in backend.waiting:
                    backend.waiting.remove(waiter)
                self._cond.notify_all()
                raise

    def start(self, name: str, profile: str | None = None, variables: dict[str, str] | None = None,
              slots: int | None = None) -> None:
        """Load a managed backend now (with new settings if given), unloading its group's
        current member, or itself when its settings change, once that is idle."""
        with self._cond:
            backend = self._backends[name]
            if not backend.spec.managed:
                raise ValueError(f"{name} is not managed by the gateway")
            changed = (profile not in (None, backend.profile)
                       or (variables is not None and variables != backend.variables))
            loaded = self._loaded_in_group(backend)
            if loaded is backend and backend.state in (READY, STARTING) and not changed:
                return
            if loaded is not None and loaded.state in (STARTING, STOPPING):
                raise RuntimeError(f"{loaded.spec.name} is {loaded.state}; try again when it settles")
            if loaded is not None and (loaded.busy or loaded.waiting):
                raise RuntimeError(f"{loaded.spec.name} is busy; load again once its queue is empty")
            if profile:
                backend.profile = profile
            if variables is not None:
                backend.variables = dict(variables)
            if slots:
                backend.slots = slots
            if loaded is not None:
                self._begin_stop(loaded, then_start=backend)
            else:
                self._begin_start(backend)

    def stop(self, name: str) -> None:
        with self._cond:
            backend = self._backends[name]
            if not backend.spec.managed:
                raise ValueError(f"{name} is not managed by the gateway")
            if backend.busy or backend.waiting:
                raise RuntimeError(f"{name} has {backend.busy} running and {len(backend.waiting)} queued")
            if backend.state in (READY, ERROR):
                self._begin_stop(backend)

    def set_reachable(self, name: str, reachable: bool, error: str | None = None) -> None:
        """Health-poller callback for backends the gateway does not manage."""
        with self._cond:
            backend = self._backends[name]
            if not backend.spec.managed:
                backend.state = READY if reachable else UNREACHABLE
                backend.error = None if reachable else error
                self._cond.notify_all()

    def tick(self) -> None:
        """Periodic housekeeping (idle unload); call every few seconds."""
        with self._cond:
            self._decide()

    def status(self) -> list[dict]:
        with self._cond:
            return [{
                "name": b.spec.name, "url": b.spec.url, "model": b.spec.model,
                "group": b.spec.group, "managed": b.spec.managed, "state": b.state,
                "slots_total": b.slots, "slots_busy": b.busy, "queued": len(b.waiting),
                "last_decode_tps": b.last_decode_tps, "last_prefill_tps": b.last_prefill_tps,
                "profile": b.profile, "vars": dict(b.variables), "error": b.error,
            } for b in self._backends.values()]

    # -- lease callbacks -----------------------------------------------------
    def _release(self, name: str) -> None:
        with self._cond:
            backend = self._backends[name]
            backend.busy -= 1
            backend.last_used = self._clock()
            self._decide()
            self._cond.notify_all()

    def _record(self, name: str, timings: dict | None) -> None:
        if not timings:
            return
        with self._cond:
            backend = self._backends[name]
            if timings.get("predicted_n", 0) > 8 and timings.get("predicted_per_second"):
                backend.last_decode_tps = round(float(timings["predicted_per_second"]), 2)
            if timings.get("prompt_n", 0) > 64 and timings.get("prompt_per_second"):
                backend.last_prefill_tps = round(float(timings["prompt_per_second"]), 1)

    # -- policy (call with the lock held) ------------------------------------
    def _group(self, backend: _Backend) -> list[_Backend]:
        if not backend.spec.group:
            return [backend]
        return [b for b in self._backends.values() if b.spec.group == backend.spec.group and b.spec.managed]

    def _loaded_in_group(self, backend: _Backend) -> _Backend | None:
        return next((b for b in self._group(backend) if b.state in _LOADED), None)

    def _decide(self) -> None:
        now = self._clock()
        seen_groups: set[str] = set()
        for backend in self._backends.values():
            if not backend.spec.managed:
                continue
            key = backend.spec.group or backend.spec.name
            if key in seen_groups:
                continue
            seen_groups.add(key)
            members = self._group(backend)
            loaded = next((b for b in members if b.state in _LOADED), None)
            wanting = sorted(  # a backend that failed to start waits for an explicit start
                (b for b in members if b is not loaded and b.waiting and b.state != ERROR),
                key=lambda b: b.waiting[0].enqueued,
            )
            if loaded is None:
                if wanting:
                    self._begin_start(wanting[0])
                continue
            if loaded.state in (STARTING, STOPPING):
                continue
            idle = loaded.busy == 0 and not loaded.waiting
            if wanting:
                if idle:
                    self._begin_stop(loaded, then_start=wanting[0])
                elif loaded.state == READY and now - wanting[0].waiting[0].enqueued > self._max_hold_s:
                    loaded.state = DRAINING
                elif loaded.state == DRAINING and loaded.busy == 0:
                    self._begin_stop(loaded, then_start=wanting[0])
            elif loaded.state == DRAINING:
                loaded.state = READY  # the others gave up waiting; carry on
            elif (idle and loaded.state == READY and self._idle_unload_s > 0
                  and now - loaded.last_used > self._idle_unload_s):
                self._begin_stop(loaded)

    def _begin_start(self, backend: _Backend) -> None:
        backend.state, backend.error = STARTING, None
        threading.Thread(target=self._run_start, args=(backend,), daemon=True,
                         name=f"start-{backend.spec.name}").start()

    def _begin_stop(self, backend: _Backend, then_start: _Backend | None = None) -> None:
        backend.state = STOPPING
        threading.Thread(target=self._run_stop, args=(backend, then_start), daemon=True,
                         name=f"stop-{backend.spec.name}").start()

    def _run_start(self, backend: _Backend) -> None:
        try:
            self._controller.start(backend.spec, backend.profile or "", dict(backend.variables))
            new_state, error = READY, None
        except Exception as exc:  # report through status and to the waiting requests
            new_state, error = ERROR, str(exc)[-500:]
        with self._cond:
            backend.state, backend.error = new_state, error
            backend.last_used = self._clock()
            self._cond.notify_all()

    def _run_stop(self, backend: _Backend, then_start: _Backend | None) -> None:
        try:
            self._controller.stop(backend.spec)
        finally:
            with self._cond:
                backend.state = STOPPED
                if then_start is not None:
                    self._begin_start(then_start)
                self._cond.notify_all()
