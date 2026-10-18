"""Latest-wins request scheduling for interactive experiments.

Interactive tools send a new request after every edit. Only the newest one per
*session* (one open canvas) is worth computing:

* each session has at most one queued request; a newer one replaces it and the
  replaced request fails with :class:`Superseded`;
* a running request whose kind is *preemptible* (slow kinds, e.g. a refine) is
  cancelled when its session submits again; fast kinds are allowed to finish,
  so continuous editing still shows a steady stream of fresh frames instead of
  starving;
* sessions are served first-come, first-served, one request at a time.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class Superseded(Exception):
    """A newer request from the same session replaced this one."""


class RequestCancelled(Exception):
    """The client cancelled this request."""


@dataclass
class Ticket:
    session: str
    kind: str
    payload: Any
    future: asyncio.Future
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created: float = field(default_factory=time.perf_counter)
    started: float | None = None
    cancel_reason: str | None = None   # "superseded" | "cancelled"


class LatestWinsScheduler:
    def __init__(self, execute: Callable[[Ticket], Awaitable[Any]], cancel_running: Callable[[Ticket], None],
                 preemptible: frozenset[str] | set[str] = frozenset()):
        self._execute = execute
        self._cancel_running = cancel_running
        self.preemptible = frozenset(preemptible)
        self._pending: dict[str, Ticket] = {}
        self.running: Ticket | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def queued(self) -> int:
        return len(self._pending)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for t in list(self._pending.values()):
            _fail(t, RequestCancelled("the service is shutting down"))
        self._pending.clear()

    def submit(self, session: str, kind: str, payload: Any) -> Ticket:
        old = self._pending.pop(session, None)
        if old:
            old.cancel_reason = "superseded"
            _fail(old, Superseded())
        run = self.running
        if run and run.session == session and run.kind in self.preemptible and not run.cancel_reason:
            run.cancel_reason = "superseded"
            self._cancel_running(run)
        t = Ticket(session, kind, payload, asyncio.get_running_loop().create_future())
        self._pending[session] = t
        self._wake.set()
        return t

    def cancel(self, session: str) -> int:
        """Cancels the queued and running requests of one session. Returns how many were affected."""
        n = 0
        old = self._pending.pop(session, None)
        if old:
            old.cancel_reason = "cancelled"
            _fail(old, RequestCancelled())
            n += 1
        run = self.running
        if run and run.session == session and not run.cancel_reason:
            run.cancel_reason = "cancelled"
            self._cancel_running(run)
            n += 1
        return n

    def cancel_ticket(self, t: Ticket) -> None:
        """Drops one ticket (its HTTP client went away)."""
        if self._pending.get(t.session) is t:
            self._pending.pop(t.session)
            t.cancel_reason = "cancelled"
            _fail(t, RequestCancelled())
        elif self.running is t and not t.cancel_reason:
            t.cancel_reason = "cancelled"
            self._cancel_running(t)

    def cancel_all(self, reason: str = "cancelled") -> None:
        for s in list(self._pending):
            self.cancel(s)
        run = self.running
        if run and not run.cancel_reason:
            run.cancel_reason = reason
            self._cancel_running(run)

    async def _loop(self) -> None:
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            session = next(iter(self._pending))
            t = self._pending.pop(session)
            self.running = t
            t.started = time.perf_counter()
            try:
                result = await self._execute(t)
            except asyncio.CancelledError:
                _fail(t, RequestCancelled("the service is shutting down"))
                raise
            except Exception as exc:  # noqa: BLE001
                if t.cancel_reason:
                    exc = Superseded() if t.cancel_reason == "superseded" else RequestCancelled()
                _fail(t, exc)
            else:
                if t.cancel_reason == "superseded":
                    _fail(t, Superseded())
                elif t.cancel_reason == "cancelled":
                    _fail(t, RequestCancelled())
                elif not t.future.done():
                    t.future.set_result(result)
            finally:
                self.running = None


def _fail(t: Ticket, exc: BaseException) -> None:
    if not t.future.done():
        t.future.set_exception(exc)
        t.future.exception()  # mark retrieved: abandoned tickets must not log "never retrieved"
