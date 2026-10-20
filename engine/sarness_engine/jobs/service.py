"""Persistent job queue with VRAM-aware admission control.

States: queued → running → completed | failed | cancelled.

Recovery semantics: on engine start, jobs left ``running`` by a crash or
restart are re-queued (attempt counter incremented) until ``job_max_attempts``
is exhausted, after which they fail with an explanatory error. Queued jobs are
simply resumed. Outputs are only recorded after files are fully written.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
import time
from typing import Any

from .. import __version__, hardware
from ..db import Database, new_id, now
from ..events import EventBus
from ..settings import Settings
from ..storage import Storage
from ..workflow import graph as wf
from ..workflow import templates
from .supervisor import WorkerSupervisor

log = logging.getLogger("sarness.jobs")

ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled")


class JobError(Exception):
    def __init__(self, message: str, issues: list[dict[str, Any]] | None = None, status: int = 400):
        super().__init__(message)
        self.issues = issues or []
        self.status = status


class JobService:
    def __init__(self, db: Database, bus: EventBus, settings: Settings, storage: Storage, registry, llama,
                 worker_options: dict[str, Any], gateway=None):
        self.db = db
        self.bus = bus
        self.settings = settings
        self.storage = storage
        self.registry = registry
        self.llama = llama
        self.gateway = gateway
        self._remote: dict[str, Any] | None = None   # {"job_id", "task", "cancel", "remote_id"} for a gateway job
        # Experiments that also use the GPU (set by the app); jobs take precedence over them.
        self.experiments = None
        self.supervisor = WorkerSupervisor(worker_options, self._on_worker_message, self._on_worker_crash)
        self._live: dict[str, dict[str, Any]] = {}   # in-memory progress for running jobs
        self._last_flush: dict[str, float] = {}
        self._wait_reason: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._dispatching = False

    # ----------------------------------------------------------- lifecycle
    def recover(self) -> None:
        max_attempts = int(self.settings.get("job_max_attempts"))
        for job in self.db.all("SELECT id, attempts FROM jobs WHERE status = 'running'"):
            if job["attempts"] + 1 >= max_attempts:
                self.db.update("jobs", job["id"], {"status": "failed", "finished_at": now(), "error": {
                    "message": "Interrupted by an engine restart too many times."}})
            else:
                self.db.update("jobs", job["id"], {"status": "queued", "attempts": job["attempts"] + 1,
                                                   "progress": 0, "progress_text": "Recovered after restart"})
                log.info("re-queued interrupted job %s", job["id"])

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        if self._remote and self._remote.get("task"):
            self._remote["task"].cancel()
        await asyncio.to_thread(self.supervisor.stop)

    # ----------------------------------------------------------- submission
    def model_lookup(self, model_id: str) -> dict[str, Any] | None:
        return self.registry.get(model_id)

    def build_workflow(self, kind: str, simple: dict[str, Any] | None, workflow: dict[str, Any] | None) -> dict[str, Any]:
        if workflow:
            return workflow
        if simple is None:
            raise JobError("provide either 'simple' parameters or a 'workflow'")
        if kind == "image":
            return templates.image_workflow(simple)
        if kind == "video":
            return templates.video_workflow(simple)
        raise JobError(f"unknown job kind {kind}")

    def submit(self, *, kind: str, simple: dict[str, Any] | None = None, workflow: dict[str, Any] | None = None,
               project_id: str | None = None, folder_id: str | None = None, title: str | None = None,
               retry_of: str | None = None, client_key: str | None = None) -> dict[str, Any]:
        """``client_key`` makes submission idempotent: resubmitting with the same key returns the
        job created by the first request (any status) instead of queueing a duplicate."""
        if client_key is not None:
            client_key = str(client_key)
            if not 8 <= len(client_key) <= 128:
                raise JobError("client_key must be 8-128 characters", status=400)
            existing = self.db.one("SELECT id FROM jobs WHERE client_key = ?", [client_key])
            if existing:
                return self.get(existing["id"])
        graph = self.build_workflow(kind, simple, workflow)
        issues = wf.validate(graph, self.model_lookup)
        errors = [i.to_dict() for i in issues if i.level == "error"]
        for mid in wf.referenced_models(graph):
            info = self.registry.get(mid)
            if info and not info.get("supported", True):
                errors.append({"message": f"{info['name']}: {info.get('unsupported_reason')}", "node_id": None,
                               "edge_id": None, "level": "error"})
        for fid in wf.referenced_files(graph):
            if not self.storage.get(fid):
                errors.append({"message": f"input file {fid} does not exist", "node_id": None, "edge_id": None,
                               "level": "error"})
        if errors:
            raise JobError(errors[0]["message"], errors, status=422)
        resolved = wf.resolve_seeds(graph)
        simple_resolved = None
        rep = templates.to_simple(resolved)
        if rep["representable"]:
            simple_resolved = rep["simple"]
        if self._remote_model(resolved) and not simple_resolved:
            raise JobError("Models on the server's AI gateway run only the standard simple-mode graph "
                           "(no extra nodes). Remove the custom nodes or pick a local model.", status=422)
        summary = templates.summarize(resolved)
        job = {
            "id": new_id("j_"), "kind": kind, "status": "queued", "project_id": project_id, "folder_id": folder_id,
            "title": title or (summary.get("prompt") or "")[:120] or f"{kind.title()} workflow",
            "workflow": resolved, "simple": simple_resolved, "progress": 0.0, "progress_text": "Queued",
            "attempts": 0, "vram_estimate_mb": self._estimate(resolved), "retry_of": retry_of,
            "created_at": now(),
        }
        if client_key is not None:
            job["client_key"] = client_key
        try:
            self.db.insert("jobs", job)
        except sqlite3.IntegrityError:
            # A concurrent request with the same key won the race.
            existing = self.db.one("SELECT id FROM jobs WHERE client_key = ?", [client_key])
            if existing:
                return self.get(existing["id"])
            raise
        out = self.get(job["id"])
        self.bus.publish("job.created", self._public(out))
        return out

    def _remote_model(self, graph: dict[str, Any]) -> dict[str, Any] | None:
        for mid in wf.referenced_models(graph):
            info = self.registry.get(mid)
            if info and info.get("remote"):
                return info
        return None

    def _estimate(self, graph: dict[str, Any]) -> int:
        need = 0
        for mid in wf.referenced_models(graph):
            info = self.registry.get(mid) or {}
            if info.get("category") in ("lora", "vae", "upscaler", "controlnet"):
                continue
            mode = "auto"
            for n in graph.get("nodes", []):
                if (n.get("params") or {}).get("model") == mid:
                    mode = (n.get("params") or {}).get("memory_mode", "auto")
            full, low = info.get("vram_mb"), info.get("vram_offload_mb")
            total = hardware.vram_total_mb() or 0
            if mode in ("offload", "sequential", "split") or (mode == "auto" and low and full and total < full + 600):
                v = low or full
            else:
                v = full
            need = max(need, int(v or 0))
        return need

    # -------------------------------------------------------------- queries
    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE id = ?", [job_id])
        if row and job_id in self._live:
            row.update(self._live[job_id])
        return row

    def list(self, status: str | None = None, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM jobs", []
        conds = []
        if status:
            statuses = status.split(",")
            conds.append(f"status IN ({','.join('?' * len(statuses))})")
            args += statuses
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = self.db.all(q, args)
        queued = [r["id"] for r in self.db.all("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at")]
        for r in rows:
            if r["id"] in self._live:
                r.update(self._live[r["id"]])
            r["queue_position"] = queued.index(r["id"]) + 1 if r["id"] in queued else None
            r["waiting_reason"] = self._wait_reason.get(r["id"])
        return [self._public(r, include_workflow=False) for r in rows]

    def _public(self, job: dict[str, Any] | None, include_workflow: bool = True) -> dict[str, Any]:
        if job is None:
            return {}
        out = dict(job)
        if not include_workflow:
            out.pop("workflow", None)
        return out

    def counts(self) -> dict[str, int]:
        rows = self.db.all("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def gpu_job_reason(self) -> str | None:
        """Why the GPU belongs to the job queue right now (``None`` when no job is running or waiting)."""
        if self._remote is not None:
            return "An image or video job is running on the server's AI gateway"
        if self.supervisor.busy or self._dispatching:
            return "An image or video job is using the GPU"
        if self.db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'queued'"):
            return "An image or video job is waiting for the GPU"
        return None

    # -------------------------------------------------------------- control
    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise JobError("job not found", status=404)
        if job["status"] == "queued":
            self.db.update("jobs", job_id, {"status": "cancelled", "finished_at": now(), "progress_text": "Cancelled"})
            self._emit_job(job_id)
        elif job["status"] == "running":
            self._live.setdefault(job_id, {})["progress_text"] = "Cancelling…"
            if self._remote and self._remote["job_id"] == job_id:
                self._remote["cancel"] = True
            else:
                self.supervisor.cancel(job_id)
            self._emit_job(job_id)
        return self.get(job_id)

    def retry(self, job_id: str, new_seed: bool = False) -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise JobError("job not found", status=404)
        graph = copy.deepcopy(job["workflow"])
        if new_seed:
            for n in graph.get("nodes", []):
                for k in ("seed",):
                    if k in (n.get("params") or {}) and n["type"].startswith(("sampler.", "value.seed")):
                        n["params"][k] = -1
        return self.submit(kind=job["kind"], workflow=graph, project_id=job["project_id"],
                           folder_id=job["folder_id"], title=job["title"], retry_of=job_id)

    def clear_finished(self) -> int:
        cur = self.db.execute("DELETE FROM jobs WHERE status IN ('completed','failed','cancelled') AND id NOT IN "
                              "(SELECT DISTINCT job_id FROM generations WHERE job_id IS NOT NULL)")
        return cur.rowcount

    # ----------------------------------------------------------- scheduling
    async def _loop(self) -> None:
        last_tick = 0.0
        while True:
            try:
                await asyncio.sleep(0.25)
                if not self.supervisor.busy and not self._dispatching and self._remote is None:
                    await self._dispatch_next()
                if time.time() - last_tick > 5:
                    last_tick = time.time()
                    idle = float(self.settings.get("worker_idle_unload_minutes")) * 60
                    await asyncio.to_thread(self.supervisor.tick, idle)
                    await asyncio.to_thread(self.llama.tick, float(self.settings.get("llm_idle_unload_minutes")))
                    self.supervisor.request_status()
                elif self.supervisor.busy:
                    self.supervisor.tick(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler loop error")

    async def _dispatch_next(self) -> None:
        job = self.db.one("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1")
        if not job:
            return
        if self.experiments is not None and self.experiments.gpu_active():
            # Interactive experiments hold VRAM between requests; a queued job takes the card back.
            await self.experiments.yield_gpu(f"job {job['id']}")
        ok, reason = await asyncio.to_thread(self._admit, job)
        if not ok:
            if self._wait_reason.get(job["id"]) != reason:
                self._wait_reason[job["id"]] = reason
                self.db.update("jobs", job["id"], {"progress_text": reason})
                self._emit_job(job["id"])
            return
        self._wait_reason.pop(job["id"], None)
        remote = self._remote_model(job["workflow"])
        if remote:
            self._start_remote(job, remote)
            return
        self._dispatching = True
        try:
            payload = self._payload(job)
            self.db.update("jobs", job["id"], {"status": "running", "started_at": now(), "progress": 0.0,
                                               "progress_text": "Starting GPU worker" if not self.supervisor.alive else "Starting",
                                               "error": None, "node_states": {}})
            self._live[job["id"]] = {"progress": 0.0, "progress_text": "Starting", "node_states": {}}
            self._emit_job(job["id"])
            try:
                await asyncio.to_thread(self.supervisor.run, payload)
            except Exception as exc:
                log.exception("failed to dispatch job %s", job["id"])
                self._finish(job["id"], "failed", error={"message": f"Could not start the GPU worker: {exc}"})
        finally:
            self._dispatching = False

    # ------------------------------------------------------- gateway jobs
    def _start_remote(self, job: dict[str, Any], info: dict[str, Any]) -> None:
        job_id = job["id"]
        self.db.update("jobs", job_id, {"status": "running", "started_at": now(), "progress": 0.0,
                                        "progress_text": "Sending to the server's AI gateway", "error": None,
                                        "node_states": {}})
        self._live[job_id] = {"progress": 0.0, "progress_text": "Sending to the server's AI gateway", "node_states": {}}
        self._emit_job(job_id)
        self._remote = {"job_id": job_id, "cancel": False, "remote_id": None}
        self._remote["task"] = asyncio.get_running_loop().create_task(self._run_remote(job, info))

    async def _run_remote(self, job: dict[str, Any], info: dict[str, Any]) -> None:
        job_id = job["id"]
        state = self._remote
        t0 = time.time()
        try:
            if not self.gateway or not self.gateway.configured:
                raise RuntimeError("The server's AI gateway is not configured (Settings → Engine → AI gateway).")
            # The gateway shares this GPU: free anything Sarness itself holds first.
            if self.supervisor.alive and not self.supervisor.busy:
                await asyncio.to_thread(self.supervisor.stop)
            if self.llama.status()["state"] in ("ready", "loading") and not self.llama.busy:
                await asyncio.to_thread(self.llama.unload)
                self.bus.publish("llm.status", self.llama.status())
            files = {}
            for fid in wf.referenced_files(job["workflow"]):
                row = self.storage.get(fid)
                if row:
                    files[fid] = str(self.storage.resolve(row["path"]))

            def progress(frac, text, step=None, total=None):
                self._on_worker_message({"type": "progress", "job_id": job_id, "progress": float(frac), "text": text,
                                         "node_id": "sampler", "step": step, "total": total})

            def set_remote_id(rid):
                state["remote_id"] = rid

            outputs = await self.gateway.run(
                kind=job["kind"], gateway_id=(info.get("meta") or {}).get("gateway_id") or info["id"].split(":", 1)[-1],
                simple=job["simple"] or {}, files=files, storage=self.storage, progress=progress,
                cancelled=lambda: state["cancel"], set_remote_id=set_remote_id)
            if state["cancel"] or not outputs:
                self._on_worker_message({"type": "cancelled", "job_id": job_id})
                return
            for o in outputs:
                o["lineage"].setdefault("model_id", info["id"])
            self._on_worker_message({"type": "done", "job_id": job_id, "outputs": outputs,
                                     "elapsed_s": round(time.time() - t0, 2), "peak_vram_mb": None})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("gateway job %s failed: %s", job_id, exc)
            self._on_worker_message({"type": "failed", "job_id": job_id, "message": str(exc) or exc.__class__.__name__,
                                     "node_id": "sampler"})
        finally:
            self._remote = None

    def _admit(self, job: dict[str, Any]) -> tuple[bool, str | None]:
        need = int(job.get("vram_estimate_mb") or 0)
        headroom = int(self.settings.get("vram_headroom_mb"))
        g = hardware.gpus()
        if not g or need == 0:
            return True, None
        free = g[0]["vram_free_mb"]
        # VRAM held by the worker's own model cache can be reclaimed by the worker itself.
        reclaimable = sum(m.get("vram_mb", 0) for m in (self.supervisor.last_status.get("loaded") or []))
        llm = self.llama.status()
        if free + reclaimable >= need + headroom or llm["state"] not in ("ready", "loading"):
            return True, None
        if self.llama.busy:
            return False, "Waiting for the chat model to finish before using the GPU"
        if self.settings.get("auto_unload_llm_for_generation"):
            log.info("unloading LLM to admit job %s (needs ~%d MiB, %d MiB free)", job["id"], need, free)
            self.llama.unload()
            self.bus.publish("llm.status", self.llama.status())
            return True, None
        return True, None  # run anyway; the worker falls back to offloading

    def _payload(self, job: dict[str, Any]) -> dict[str, Any]:
        graph = job["workflow"]
        models = {}
        for mid in wf.referenced_models(graph):
            info = self.registry.worker_info(mid)
            if info:
                models[mid] = info
        files = {}
        for fid in wf.referenced_files(graph):
            row = self.storage.get(fid)
            if row:
                files[fid] = str(self.storage.resolve(row["path"]))
        return {"job_id": job["id"], "graph": graph, "models": models, "files": files, "attempt": job["attempts"]}

    # ------------------------------------------------------- worker events
    def _emit_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if job:
            pub = self._public(job, include_workflow=False)
            pub["waiting_reason"] = self._wait_reason.get(job_id)
            self.bus.publish("job.updated", pub)

    def _on_worker_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        job_id = msg.get("job_id")
        if t == "status":
            self.bus.publish("worker.status", {k: v for k, v in msg.items() if k != "type"})
            return
        if not job_id:
            return
        live = self._live.setdefault(job_id, {"node_states": {}})
        if t == "progress":
            live.update(progress=msg["progress"], progress_text=msg.get("text"), current_node=msg.get("node_id"))
            self.bus.publish("job.progress", {"id": job_id, "progress": msg["progress"], "text": msg.get("text"),
                                              "node_id": msg.get("node_id"), "step": msg.get("step"),
                                              "total": msg.get("total")})
            if time.time() - self._last_flush.get(job_id, 0) > 1.5:
                self._last_flush[job_id] = time.time()
                self.db.update("jobs", job_id, {"progress": msg["progress"], "progress_text": msg.get("text"),
                                                "current_node": msg.get("node_id")})
        elif t == "preview":
            self.bus.publish("job.preview", {"id": job_id, "node_id": msg.get("node_id"), "image": msg["image"]})
        elif t == "node":
            states = live.setdefault("node_states", {})
            states[msg["node_id"]] = {k: v for k, v in msg.items() if k not in ("type", "job_id", "node_id")}
            if msg["status"] == "running":
                live["current_node"] = msg["node_id"]
            self.bus.publish("job.node", {"id": job_id, "node_id": msg["node_id"], **states[msg["node_id"]]})
        elif t == "done":
            try:
                gens = self._record_outputs(job_id, msg)
            except Exception as exc:
                log.exception("failed to record outputs for %s", job_id)
                self._finish(job_id, "failed", error={"message": f"Outputs could not be saved: {exc}"})
                return
            self._finish(job_id, "completed", result={
                "generation_ids": [g["id"] for g in gens], "elapsed_s": msg.get("elapsed_s"),
                "peak_vram_mb": msg.get("peak_vram_mb")}, node_states=msg.get("node_states"))
            for g in gens:
                self.bus.publish("generation.created", g)
        elif t == "failed":
            self._handle_failure(job_id, msg)
        elif t == "cancelled":
            self._finish(job_id, "cancelled", progress_text="Cancelled" + (" (worker restarted)" if msg.get("forced") else ""))

    def _handle_failure(self, job_id: str, msg: dict[str, Any]) -> None:
        job = self.get(job_id)
        if not job:
            return
        if msg.get("oom") and self.settings.get("oom_retry_with_offload") and job["attempts"] < 1:
            graph = copy.deepcopy(job["workflow"])
            changed = False
            for n in graph.get("nodes", []):
                p = n.get("params") or {}
                if n["type"] in ("loader.image_model", "loader.video_model") and p.get("memory_mode") in ("auto", "gpu"):
                    p["memory_mode"] = "offload"
                    changed = True
            if changed:
                log.info("job %s hit OOM; retrying with CPU offload", job_id)
                self._live.pop(job_id, None)
                self.db.update("jobs", job_id, {"status": "queued", "workflow": graph, "attempts": job["attempts"] + 1,
                                                "progress": 0, "progress_text": "Out of GPU memory — retrying with offload",
                                                "started_at": None})
                self._emit_job(job_id)
                return
        self._finish(job_id, "failed", error={"message": msg.get("message"), "node_id": msg.get("node_id"),
                                              "oom": bool(msg.get("oom")), "hint": msg.get("hint")})

    def _on_worker_crash(self, job_id: str | None, message: str) -> None:
        if not job_id:
            self.bus.publish("worker.crashed", {"message": message})
            return
        job = self.get(job_id)
        if job and job["attempts"] + 1 < int(self.settings.get("job_max_attempts")):
            self._live.pop(job_id, None)
            self.db.update("jobs", job_id, {"status": "queued", "attempts": job["attempts"] + 1, "progress": 0,
                                            "progress_text": "GPU worker crashed — retrying"})
            self._emit_job(job_id)
        else:
            self._finish(job_id, "failed", error={"message": message})

    def _finish(self, job_id: str, status: str, *, error: dict | None = None, result: dict | None = None,
                node_states: dict | None = None, progress_text: str | None = None) -> None:
        live = self._live.pop(job_id, {})
        values: dict[str, Any] = {"status": status, "finished_at": now(),
                                  "node_states": node_states or live.get("node_states") or {}}
        if status == "completed":
            values.update(progress=1.0, progress_text="Completed", result=result)
        else:
            values.update(progress=live.get("progress", 0), progress_text=progress_text or status.title())
        if error:
            values["error"] = error
        self.db.update("jobs", job_id, values)
        self._last_flush.pop(job_id, None)
        self._emit_job(job_id)

    def _record_outputs(self, job_id: str, msg: dict[str, Any]) -> list[dict[str, Any]]:
        from pathlib import Path

        job = self.db.one("SELECT * FROM jobs WHERE id = ?", [job_id])
        gens = []
        for out in msg.get("outputs") or []:
            path = Path(out["path"])
            kind = out["kind"]
            f = self.storage.register(path, kind, mime=out.get("mime"), width=out.get("width"),
                                      height=out.get("height"), duration=out.get("duration"))
            thumb = (self.storage.make_image_thumb(path) if kind == "image" else self.storage.make_video_thumb(path))
            lin = out.get("lineage") or {}
            simple = dict(job.get("simple") or {}) or None
            gen = {
                "id": new_id("g_"), "job_id": job_id, "type": kind, "project_id": job.get("project_id"),
                "folder_id": job.get("folder_id"), "file_id": f["id"], "thumb_file_id": thumb["id"] if thumb else None,
                "prompt": lin.get("prompt"), "negative_prompt": lin.get("negative_prompt"),
                "model_id": lin.get("model_id"), "seed": lin.get("seed"),
                "width": out.get("width"), "height": out.get("height"), "frames": out.get("frames"),
                "fps": out.get("fps"), "duration": out.get("duration"),
                "metadata": {**lin, "batch_index": out.get("batch_index"), "elapsed_s": msg.get("elapsed_s"),
                             "peak_vram_mb": msg.get("peak_vram_mb"), "engine_version": __version__,
                             "node_id": out.get("node_id")},
                "workflow": job["workflow"], "simple": simple, "favorite": 0, "created_at": now(),
            }
            self.db.insert("generations", gen)
            gens.append(self.generation_public(gen))
        return gens

    def generation_public(self, g: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in g.items() if k != "workflow"}
        out["url"] = self.storage.file_url(g["file_id"])
        out["thumb_url"] = self.storage.file_url(g.get("thumb_file_id")) or out["url"]
        return out
