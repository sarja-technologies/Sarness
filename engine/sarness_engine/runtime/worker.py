"""GPU worker process.

Spawned by :class:`~sarness_engine.jobs.supervisor.WorkerSupervisor`. Everything
that touches CUDA lives here, so a driver fault, segfault or unrecoverable OOM
kills only this process; the API keeps serving and the supervisor restarts it.

Protocol (dicts over a multiprocessing Pipe):

  parent → worker: run, cancel, release, status, shutdown
  worker → parent: ready, progress, preview, node, done, failed, cancelled, status, log
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any


class _Services:
    def __init__(self, models, outputs_dir: Path):
        self.models = models
        self.outputs_dir = outputs_dir
        self._model_index: dict[str, dict[str, Any]] = {}
        self._files: dict[str, str] = {}

    def bind(self, model_index: dict[str, dict[str, Any]], files: dict[str, str]) -> None:
        self._model_index = model_index
        self._files = files

    def model_info(self, model_id: str) -> dict[str, Any] | None:
        return self._model_index.get(model_id)

    def file_path(self, file_id: str) -> str | None:
        p = self._files.get(file_id)
        return p if p and os.path.exists(p) else None

    def output_path(self, kind: str, ext: str) -> Path:
        from ..db import new_id

        t = time.localtime()
        d = self.outputs_dir / ("images" if kind == "image" else "videos") / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"sarness-{time.strftime('%Y%m%d-%H%M%S', t)}-{new_id()[:8]}.{ext}"


def worker_main(conn, options: dict[str, Any]) -> None:  # pragma: no cover - exercised via integration tests
    log_dir = Path(options["logs_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s worker %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_dir / "worker.log"), logging.StreamHandler()],
    )
    log = logging.getLogger("sarness.worker")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # workers never download; the API does

    send_lock = threading.Lock()

    def send(msg: dict[str, Any]) -> None:
        with send_lock:
            try:
                conn.send(msg)
            except (BrokenPipeError, OSError):
                pass

    import torch

    from ..workflow.executor import NodeCache, execute
    from ..workflow.types import Cancelled, WorkflowError
    from .models import ModelManager, cuda_available, free_vram_mb, total_vram_mb

    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    models = ModelManager(headroom_mb=int(options.get("headroom_mb", 600)))
    services = _Services(models, Path(options["outputs_dir"]))
    cache = NodeCache()
    # Cached node outputs hold model handles: once a model is evicted (also to make room for another) its
    # weights are gone, so a cached handle must never be reused.
    models.on_evict.append(lambda key: cache.clear())
    jobs: queue.Queue = queue.Queue()
    cancel_events: dict[str, threading.Event] = {}
    stop = threading.Event()

    def status() -> dict[str, Any]:
        return {"type": "status", "loaded": models.status(), "cache_entries": len(cache),
                "vram_free_mb": free_vram_mb(), "vram_total_mb": total_vram_mb(),
                "torch_vram_allocated_mb": (torch.cuda.memory_allocated() // 2**20) if cuda_available() else 0,
                "torch_vram_reserved_mb": (torch.cuda.memory_reserved() // 2**20) if cuda_available() else 0}

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
                cancel_events[msg["job_id"]] = threading.Event()
                jobs.put(msg)
            elif t == "cancel":
                ev = cancel_events.get(msg["job_id"])
                if ev:
                    ev.set()
            elif t == "release":
                jobs.put({"type": "release", "key": msg.get("key")})
            elif t == "status":
                send(status())
            elif t == "shutdown":
                stop.set()
                jobs.put(None)
                return

    threading.Thread(target=reader, daemon=True, name="worker-reader").start()
    send({"type": "ready", "pid": os.getpid(), "torch": torch.__version__, "cuda": torch.version.cuda,
          "cuda_available": cuda_available(),
          "device": torch.cuda.get_device_name(0) if cuda_available() else "cpu"})
    log.info("worker ready (torch %s, cuda %s)", torch.__version__, torch.version.cuda)

    while not stop.is_set():
        msg = jobs.get()
        if msg is None:
            break
        if msg.get("type") == "release":
            if msg.get("key"):
                models.evict(msg["key"])
            else:
                models.unload_all()
            cache.clear()  # cached node outputs can hold the released model's tensors
            send(status())
            continue
        job_id = msg["job_id"]
        ev = cancel_events[job_id]
        services.bind(msg.get("models") or {}, msg.get("files") or {})
        t0 = time.perf_counter()
        if cuda_available():
            torch.cuda.reset_peak_memory_stats()

        def emit(event: dict[str, Any]) -> None:
            send(event)

        try:
            result = execute(msg["graph"], job_id=job_id, services=services, emit=emit, cancel_event=ev, cache=cache)
            peak = (torch.cuda.max_memory_allocated() // 2**20) if cuda_available() else 0
            send({"type": "done", "job_id": job_id, "outputs": result["outputs"], "node_states": result["node_states"],
                  "elapsed_s": round(time.perf_counter() - t0, 2), "peak_vram_mb": peak,
                  "loaded": models.status()})
        except Cancelled:
            send({"type": "cancelled", "job_id": job_id})
        except WorkflowError as exc:
            oom_hit = exc.oom
            failure = {"type": "failed", "job_id": job_id, "message": str(exc), "node_id": exc.node_id,
                       "oom": exc.oom, "hint": exc.hint}
            # Drop the traceback: its frames pin pipelines and tensors in VRAM.
            exc.__traceback__ = None
            if exc.__cause__ is not None:
                exc.__cause__.__traceback__ = None
            del exc
            if oom_hit:
                log.warning("OOM in job %s; releasing all models", job_id)
                cache.clear()
                models.unload_all()
                log.info("after release: %d MiB allocated", torch.cuda.memory_allocated() // 2**20 if cuda_available() else 0)
            send(failure)
        except Exception as exc:  # noqa: BLE001
            log.error("unexpected worker error: %s\n%s", exc, traceback.format_exc())
            send({"type": "failed", "job_id": job_id, "message": f"Internal error: {exc}", "node_id": None, "oom": False})
        finally:
            cancel_events.pop(job_id, None)
            if cuda_available():
                torch.cuda.empty_cache()
    log.info("worker exiting")
    models.unload_all()
