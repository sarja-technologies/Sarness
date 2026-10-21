"""Supervises the GPU worker process: spawn, message routing, crash detection,
hard-cancel escalation and idle shutdown (which returns all VRAM to the system)."""

from __future__ import annotations

import logging
import multiprocessing as mp
import signal
import threading
import time
from typing import Any, Callable

log = logging.getLogger("sarness.supervisor")

HARD_CANCEL_AFTER_S = 25.0


def memory_floor_mb() -> int:
    """System memory below which the GPU worker is stopped: 3 % of RAM, between 4 and 8 GiB."""
    import psutil

    return int(min(max(psutil.virtual_memory().total // 2**20 * 3 // 100, 4096), 8192))


class WorkerSupervisor:
    def __init__(self, options: dict[str, Any], on_message: Callable[[dict[str, Any]], None],
                 on_crash: Callable[[str | None, str, bool], None]):
        self.options = options
        self.on_message = on_message
        self.on_crash = on_crash
        self._ctx = mp.get_context("spawn")
        self._proc: mp.Process | None = None
        self._conn = None
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self.current_job: str | None = None
        self.info: dict[str, Any] = {}
        self.last_status: dict[str, Any] = {}
        self.last_active = time.time()
        self._ready = threading.Event()
        self._cancel_deadline: float | None = None
        self._expected_exit = False
        self._status = threading.Event()

    # ------------------------------------------------------------- lifecycle
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    @property
    def busy(self) -> bool:
        return self.current_job is not None

    def start(self, timeout: float = 120.0) -> None:
        with self._lock:
            if self.alive:
                return
            from ..runtime.worker import worker_main

            parent, child = self._ctx.Pipe(duplex=True)
            self._ready.clear()
            self._expected_exit = False
            self._proc = self._ctx.Process(target=worker_main, args=(child, self.options), name="sarness-gpu-worker",
                                           daemon=True)
            self._proc.start()
            child.close()
            self._conn = parent
            self._reader = threading.Thread(target=self._read_loop, args=(parent, self._proc), daemon=True,
                                            name="supervisor-reader")
            self._reader.start()
            threading.Thread(target=self._guard_memory, args=(self._proc,), daemon=True,
                             name="supervisor-memory-guard").start()
            log.info("spawned GPU worker pid=%s", self._proc.pid)
        if not self._ready.wait(timeout):
            raise RuntimeError("GPU worker did not become ready")
        if not self.alive:
            raise RuntimeError("GPU worker failed to start (see logs/worker.log)")

    def _guard_memory(self, proc, interval: float = 0.25) -> None:
        """Stops the worker before the machine runs out of memory. On unified-memory systems GPU allocations are
        invisible to the kernel's OOM killer (they are not in any process's RSS) and are not charged to the
        engine's cgroup, so without this the kernel kills desktop and system services first."""
        import psutil

        floor = memory_floor_mb()
        while proc.is_alive():
            avail = psutil.virtual_memory().available // 2**20
            if avail < floor:
                log.error("only %d MB of system memory left (floor %d MB): stopping the GPU worker", avail, floor)
                proc.kill()
                return
            time.sleep(interval)

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            # A local reference: the reader thread clears self._proc as soon as the worker exits.
            proc = self._proc
            if not proc:
                return
            self._expected_exit = True
            try:
                self._conn.send({"type": "shutdown"})
            except Exception:
                pass
            proc.join(timeout)
            if proc.is_alive():
                proc.kill()
                proc.join(5)
            self._proc = None
            self.info = {}
            self.last_status = {}
            log.info("GPU worker stopped")

    def _send(self, msg: dict[str, Any]) -> None:
        with self._lock:
            if not self.alive:
                raise RuntimeError("GPU worker is not running")
            self._conn.send(msg)

    # --------------------------------------------------------------- control
    def run(self, payload: dict[str, Any]) -> None:
        self.start()
        self.current_job = payload["job_id"]
        self.last_active = time.time()
        self._cancel_deadline = None
        self._send({"type": "run", **payload})

    def cancel(self, job_id: str) -> None:
        if self.current_job != job_id:
            return
        try:
            self._send({"type": "cancel", "job_id": job_id})
        except RuntimeError:
            return
        self._cancel_deadline = time.time() + HARD_CANCEL_AFTER_S

    def release(self, key: str | None = None, wait: float = 0.0) -> None:
        """Unloads one cached model (``key``) or all of them. With ``wait`` it blocks until the worker
        reports its new status, so ``last_status`` is current when it returns."""
        if self.alive and not self.busy:
            self._status.clear()
            self._send({"type": "release", "key": key})
            if wait:
                self._status.wait(wait)

    def request_status(self) -> None:
        if self.alive:
            try:
                self._send({"type": "status"})
            except Exception:
                pass

    def tick(self, idle_unload_s: float) -> None:
        """Called periodically: escalate stuck cancels, idle-stop the worker."""
        if self._cancel_deadline and time.time() > self._cancel_deadline and self.current_job:
            log.warning("job %s ignored cancel; killing worker", self.current_job)
            job = self.current_job
            self._expected_exit = True
            with self._lock:
                if self._proc:
                    self._proc.kill()
            self._cancel_deadline = None
            self.current_job = None
            self.on_message({"type": "cancelled", "job_id": job, "forced": True})
        if self.alive and not self.busy and idle_unload_s > 0 and time.time() - self.last_active > idle_unload_s:
            log.info("GPU worker idle for %.0fs; stopping to free VRAM", idle_unload_s)
            self.stop()

    # ---------------------------------------------------------------- reader
    def _read_loop(self, conn, proc) -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            t = msg.get("type")
            if t == "ready":
                self.info = msg
                self._ready.set()
                continue
            if t == "status":
                self.last_status = msg
                self._status.set()
            if t in ("done", "failed", "cancelled"):
                if msg.get("job_id") == self.current_job:
                    self.current_job = None
                    self._cancel_deadline = None
                self.last_active = time.time()
                if t == "done":
                    self.last_status = {**self.last_status, "loaded": msg.get("loaded", [])}
            try:
                self.on_message(msg)
            except Exception:  # pragma: no cover
                log.exception("error handling worker message %s", t)
        proc.join(2)
        code = proc.exitcode
        with self._lock:
            if self._proc is not None and self._proc is not proc:
                return  # a newer worker has already taken over
            self._proc = None
        job = self.current_job
        self.current_job = None
        self.info = {}
        self.last_status = {}
        self._ready.set()
        if not self._expected_exit:
            log.error("GPU worker exited unexpectedly (code %s)", code)
            if code == -signal.SIGKILL:
                # Almost always the kernel's out-of-memory killer: running the same job again would repeat it.
                self.on_crash(job, "The GPU worker was stopped by the system for running out of memory. Try a "
                                   "smaller size, fewer frames or unloading other models.", False)
            else:
                self.on_crash(job, f"GPU worker exited unexpectedly (exit code {code})", True)
