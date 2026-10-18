"""Crash-isolated GPU worker process for experiments.

Same reasoning as the main GPU worker (:mod:`sarness_engine.runtime.worker`):
CUDA lives in a spawned child process, so a driver fault or runaway allocation
kills only that process and stopping it returns *all* of its VRAM. Unlike the
job worker it keeps no queue of its own and runs short, latency-sensitive
requests; scheduling (latest-wins, cancellation) happens in the API process.

Any experiment can use it by naming a handler class ``"package.module:Class"``
with ``run(op, payload, cancelled, progress)``, ``release(level)`` and
``status()``. A module-level ``errors()`` may map exception types to wire codes.

Protocol (dicts over a multiprocessing Pipe)::

  parent → child: run {req_id, op, payload} · cancel {req_id} · release {level} · status {req_id} · shutdown
  child → parent: ready · progress {req_id, step, total} · result {req_id, data} ·
                  error {req_id, code, message, info} · status {req_id, data}
"""

from __future__ import annotations

import concurrent.futures as cf
import importlib
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("sarness.experiments.worker")


class WorkerError(Exception):
    def __init__(self, code: str, message: str, info: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.info = info or {}


class ExperimentWorker:
    def __init__(self, name: str, handler: str, options: dict[str, Any], logs_dir: Path,
                 on_event: Callable[[dict[str, Any]], None]):
        self.name = name
        self.handler = handler
        self.options = options
        self.logs_dir = Path(logs_dir)
        self.on_event = on_event
        self._ctx = mp.get_context("spawn")
        self._proc: mp.Process | None = None
        self._conn = None
        self._lock = threading.RLock()
        self._pending: dict[str, cf.Future] = {}
        self._ready = threading.Event()
        self._expected_exit = False
        self.info: dict[str, Any] = {}
        self.last_status: dict[str, Any] = {}
        self.last_active = time.time()
        self.started_at: float | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    @property
    def busy(self) -> bool:
        return any(not f.done() and k.startswith("r") for k, f in list(self._pending.items()))

    # ------------------------------------------------------------ lifecycle
    def start(self, timeout: float = 120.0) -> None:
        with self._lock:
            if self.alive:
                return
            parent, child = self._ctx.Pipe(duplex=True)
            self._ready.clear()
            self._expected_exit = False
            spec = {"name": self.name, "handler": self.handler, "options": self.options, "logs_dir": str(self.logs_dir)}
            self._proc = self._ctx.Process(target=experiment_worker_main, args=(child, spec),
                                           name=f"sarness-exp-{self.name}", daemon=True)
            self._proc.start()
            child.close()
            self._conn = parent
            threading.Thread(target=self._read_loop, args=(parent, self._proc), daemon=True,
                             name=f"exp-{self.name}-reader").start()
            log.info("spawned experiment worker %s pid=%s", self.name, self._proc.pid)
        if not self._ready.wait(timeout) or not self.alive:
            self.stop()
            raise WorkerError("start", f"the {self.name} worker did not start (see logs/experiment-{self.name}.log)")
        self.started_at = time.time()
        self.last_active = time.time()
        self.on_event({"type": "started", "info": self.info})

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
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
            self.info, self.last_status, self.started_at = {}, {}, None
        self._fail_pending(WorkerError("stopped", "the worker was stopped"))
        log.info("experiment worker %s stopped", self.name)
        self.on_event({"type": "stopped"})

    def _send(self, msg: dict[str, Any]) -> None:
        with self._lock:
            if not self.alive:
                raise WorkerError("stopped", f"the {self.name} worker is not running")
            self._conn.send(msg)

    # --------------------------------------------------------------- calls
    def submit(self, op: str, payload: dict[str, Any], req_id: str | None = None) -> cf.Future:
        """Starts the worker if needed and sends one request; the future resolves with its result."""
        self.start()
        rid = "r" + (req_id or uuid.uuid4().hex)
        fut: cf.Future = cf.Future()
        self._pending[rid] = fut
        self.last_active = time.time()
        try:
            self._send({"type": "run", "req_id": rid, "op": op, "payload": payload})
        except WorkerError as exc:
            self._pending.pop(rid, None)
            fut.set_exception(exc)
        return fut

    def cancel(self, req_id: str) -> None:
        try:
            self._send({"type": "cancel", "req_id": "r" + req_id})
        except WorkerError:
            pass

    def release(self, level: str = "all") -> None:
        if self.alive:
            try:
                self._send({"type": "release", "level": level})
            except WorkerError:
                pass

    def query_status(self, timeout: float = 3.0) -> dict[str, Any]:
        if not self.alive:
            return {}
        rid = "s" + uuid.uuid4().hex
        fut: cf.Future = cf.Future()
        self._pending[rid] = fut
        try:
            self._send({"type": "status", "req_id": rid})
            self.last_status = fut.result(timeout)
        except Exception:
            pass
        finally:
            self._pending.pop(rid, None)
        return self.last_status

    # -------------------------------------------------------------- reader
    def _fail_pending(self, exc: Exception) -> None:
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
            self._pending.pop(rid, None)

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
            elif t in ("result", "error", "status"):
                fut = self._pending.pop(msg.get("req_id", ""), None)
                if t == "status":
                    self.last_status = msg.get("data") or {}
                elif t == "result":
                    self.last_active = time.time()
                if fut and not fut.done():
                    if t == "error":
                        fut.set_exception(WorkerError(msg.get("code") or "error", msg.get("message") or "worker error",
                                                      msg.get("info")))
                    else:
                        fut.set_result(msg.get("data"))
            elif t == "progress":
                self.last_active = time.time()
                try:
                    self.on_event({"type": "progress", "req_id": msg["req_id"][1:], "step": msg.get("step"),
                                   "total": msg.get("total")})
                except Exception:  # pragma: no cover
                    log.exception("progress handler failed")
        proc.join(2)
        if self._proc is not None and self._proc is not proc:
            return
        code = proc.exitcode
        self._proc = None
        self.info, self.last_status, self.started_at = {}, {}, None
        self._ready.set()
        crashed = not self._expected_exit
        self._fail_pending(WorkerError("crashed", f"the {self.name} worker exited unexpectedly (exit code {code})")
                           if crashed else WorkerError("stopped", "the worker was stopped"))
        if crashed:
            log.error("experiment worker %s exited unexpectedly (code %s)", self.name, code)
            self.on_event({"type": "crashed", "exit_code": code})


# ======================================================================== child
def experiment_worker_main(conn, spec: dict[str, Any]) -> None:  # pragma: no cover - exercised by GPU tests
    name = spec["name"]
    log_dir = Path(spec["logs_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s exp-{name} %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(log_dir / f"experiment-{name}.log"), logging.StreamHandler()])
    wlog = logging.getLogger(f"sarness.experiments.{name}")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    send_lock = threading.Lock()

    def send(msg: dict[str, Any]) -> None:
        with send_lock:
            try:
                conn.send(msg)
            except (BrokenPipeError, OSError):
                pass

    mod_name, cls_name = spec["handler"].split(":")
    mod = importlib.import_module(mod_name)
    codes: dict[type, str] = mod.errors() if hasattr(mod, "errors") else {}
    handler = getattr(mod, cls_name)(spec["options"], send)
    jobs: queue.Queue = queue.Queue()
    cancels: dict[str, threading.Event] = {}
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                stop.set()
                jobs.put(None)
                return
            t = msg.get("type")
            if t == "run":
                cancels[msg["req_id"]] = threading.Event()
                jobs.put(msg)
            elif t == "cancel":
                ev = cancels.get(msg["req_id"])
                if ev:
                    ev.set()
            elif t == "release":
                jobs.put(msg)
            elif t == "status":
                try:
                    send({"type": "status", "req_id": msg["req_id"], "data": handler.status()})
                except Exception as exc:  # noqa: BLE001
                    send({"type": "status", "req_id": msg["req_id"], "data": {"error": str(exc)}})
            elif t == "shutdown":
                stop.set()
                jobs.put(None)
                return

    threading.Thread(target=reader, daemon=True, name="exp-reader").start()
    try:
        import torch
    except ImportError:  # handlers that need no GPU (and the test suite) run without torch
        torch = None
    cuda = bool(torch and torch.cuda.is_available())
    send({"type": "ready", "pid": os.getpid(), "torch": torch.__version__ if torch else None,
          "cuda": torch.version.cuda if torch else None, "device": torch.cuda.get_device_name(0) if cuda else "cpu"})
    wlog.info("worker ready (torch %s)", torch.__version__ if torch else "not installed")
    while not stop.is_set():
        msg = jobs.get()
        if msg is None:
            break
        if msg["type"] == "release":
            handler.release(msg.get("level", "all"))
            continue
        rid = msg["req_id"]
        ev = cancels[rid]
        try:
            data = handler.run(msg["op"], msg.get("payload") or {}, ev.is_set,
                               lambda step, total, _rid=rid: send({"type": "progress", "req_id": _rid, "step": step,
                                                                   "total": total}))
            send({"type": "result", "req_id": rid, "data": data})
        except Exception as exc:  # noqa: BLE001
            code = next((c for t, c in codes.items() if isinstance(exc, t)), None)
            oom = bool(torch) and isinstance(exc, torch.cuda.OutOfMemoryError)
            if oom:
                code = "oom"
            info = {k: v for k, v in vars(exc).items() if isinstance(v, (int, float, str, bool))}
            if code is None:
                wlog.exception("request %s failed", rid)
            send({"type": "error", "req_id": rid, "code": code or "error", "message": str(exc) or exc.__class__.__name__,
                  "info": info})
            exc.__traceback__ = None
            del exc
            if oom:  # drop the large models; a handler may keep its small ones with on_oom()
                getattr(handler, "on_oom", lambda: handler.release("all"))()
            if cuda:
                torch.cuda.empty_cache()
        finally:
            cancels.pop(rid, None)
    wlog.info("worker exiting")
    try:
        handler.release("all")
    except Exception:
        pass
