"""God's Paintbrush — API-side service: scheduling, GPU sharing, renders, exports.

Requests flow: HTTP → :class:`~..scheduler.LatestWinsScheduler` (one queued
request per canvas session, stale refines cancelled) → the experiment worker
process. The GPU is shared politely: Sarness image/video jobs always win
(queued jobs make the paintbrush stand down and free its VRAM), idle chat
models are unloaded only when VRAM is short and no chat reply is streaming,
and idle models are released after a few minutes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from statistics import median
from typing import Any

from ... import __version__, hardware
from ...db import new_id, now
from ..registry import Experiment
from ..scheduler import LatestWinsScheduler, RequestCancelled, Ticket
from ..worker import ExperimentWorker, WorkerError
from . import components as C
from . import masks as MK
from . import materials as M

log = logging.getLogger("sarness.experiments.paintbrush")

EXPERIMENT_ID = "gods-paintbrush"
HANDLER = "sarness_engine.experiments.paintbrush.worker:PaintbrushHandler"

# Canvas sizes are native to the live model (multiples of 32, trained at 512 px).
CANVAS_SIZES = [
    {"id": "landscape", "label": "Landscape 3:2", "width": 768, "height": 512},
    {"id": "square", "label": "Square 1:1", "width": 512, "height": 512},
    {"id": "wide", "label": "Wide 7:4", "width": 896, "height": 512},
    {"id": "portrait", "label": "Portrait 2:3", "width": 512, "height": 768},
]
# Refine output scales offered after measuring them on a 12 GB RTX 4070 Ti (docs/EXPERIMENTS.md).
REFINE_SCALES = [1.0, 1.5, 2.0]
MAX_REFINE_PIXELS = 1536 * 1024
QUALITY_STEPS = {"draft": 10, "standard": 20, "high": 30}
# Below ~0.8 the refine keeps the live frame's colours, so text guidance mostly sharpens detail; at 0.85+ it can
# change season and light while the painted layout holds (measured sweep in docs/EXPERIMENTS.md).
DEFAULT_CREATIVITY = 0.7
RENDER_CACHE = 12


class PaintbrushError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GpuBusy(PaintbrushError):
    def __init__(self, reason: str):
        super().__init__(reason, 503)


def refine_size(width: int, height: int, scale: float) -> tuple[int, int]:
    if scale not in REFINE_SCALES:
        raise PaintbrushError(f"scale must be one of {REFINE_SCALES}")
    ow, oh = (int(round(width * scale / 64)) * 64, int(round(height * scale / 64)) * 64)
    if ow * oh > MAX_REFINE_PIXELS:
        raise PaintbrushError(f"{ow}×{oh} is larger than the verified refine limit "
                              f"({MAX_REFINE_PIXELS // 1_000_000:.1f} MP on a 12 GB GPU)")
    return ow, oh


class Paintbrush(Experiment):
    id = EXPERIMENT_ID
    name = "God's Paintbrush"
    tagline = "Turn simple painted shapes into photorealistic landscapes using AI."
    description = ("Paint flat regions of sky, water, mountains, grass and more; a GauGAN (SPADE) landscape model "
                   "turns the semantic painting into a photo in a fraction of a second, and an optional "
                   "ControlNet-Seg refine pass adds detail, seeds and text guidance.")
    version = "1"
    capabilities = ("server-inference", "gpu", "documents", "library")

    def __init__(self, st):
        self.st = st
        self.models_dir: Path = st.cfg.models_dir
        self.worker = ExperimentWorker(EXPERIMENT_ID, HANDLER, {"paths": C.worker_paths(self.models_dir)},
                                       st.cfg.logs_dir, self._on_worker_event)
        self.installer = C.Installer(self.models_dir, lambda: st.settings.get("hf_token") or "", self._on_install)
        self.scheduler: LatestWinsScheduler | None = None
        self.renders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.recent: deque[dict[str, Any]] = deque(maxlen=200)
        self.loaded = {"live": False, "refine": False}
        self.last_error: str | None = None
        self._hold_until = 0.0
        self._tickets: dict[str, str] = {}   # running ticket id → session (for progress events)
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        self.scheduler = LatestWinsScheduler(self._execute, self._cancel_running, preemptible={"refine"})
        self.scheduler.start()
        self._tasks.append(asyncio.get_running_loop().create_task(self._housekeeping()))

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.scheduler:
            await self.scheduler.stop()
        self.installer.cancel()
        await asyncio.to_thread(self.worker.stop)

    def gpu_active(self) -> bool:
        return self.worker.alive or bool(self.scheduler and self.scheduler.running)

    async def yield_gpu(self, reason: str) -> None:
        log.info("releasing the GPU (%s)", reason)
        self._hold_until = time.time() + 10
        if self.scheduler:
            self.scheduler.cancel_all()
            deadline = time.time() + 15
            while self.scheduler.running and time.time() < deadline:
                await asyncio.sleep(0.05)
        await asyncio.to_thread(self.worker.stop)

    # ---------------------------------------------------------- availability
    def availability(self) -> dict[str, Any]:
        comps = {c.id: C.installed(self.models_dir, c) for c in C.COMPONENTS}
        live = comps["spade-landscapes-512"]
        refine = comps["sd15"] and comps["controlnet-seg"]
        reasons = []
        if not live:
            reasons.append("The GauGAN landscape model is not installed on the server.")
        if not refine:
            reasons.append("The refine models (Stable Diffusion 1.5 + ControlNet-Seg) are not installed; "
                           "live painting works without them.")
        try:
            import importlib.util

            if not importlib.util.find_spec("torch"):
                return {"state": "unavailable", "live": False, "refine": False,
                        "reasons": ["PyTorch is not installed on the server."]}
        except Exception:  # pragma: no cover
            pass
        state = "ready" if live and refine else ("partial" if live else "needs_install")
        return {"state": state, "live": live, "refine": refine, "reasons": reasons}

    def gpu_busy_reason(self) -> str | None:
        reason = self.st.jobs.gpu_job_reason()
        if reason:
            return f"{reason}. Live painting resumes when it finishes."
        if time.time() < self._hold_until:
            return "The GPU is being handed to an image or video job."
        return None

    async def gpu_busy_reason_async(self) -> str | None:
        reason = self.gpu_busy_reason()
        if reason:
            return reason
        gw = self.st.gateway
        if gw.configured and await gw.busy_job():
            return "The server's AI gateway is running a GPU job for another client. Live painting resumes when it finishes."
        return None

    # --------------------------------------------------------------- status
    def runtime(self, query: bool = True) -> dict[str, Any]:
        ws = self.worker.query_status() if (query and self.worker.alive) else self.worker.last_status
        sch = self.scheduler
        return {"worker": self.worker.alive, "pid": self.worker.info.get("pid"), "device": ws.get("device"),
                "loaded": dict(self.loaded) if self.worker.alive else {"live": False, "refine": False},
                "vram_allocated_mb": ws.get("vram_allocated_mb"), "vram_reserved_mb": ws.get("vram_reserved_mb"),
                "load_s": ws.get("load_s") or {}, "busy": bool(sch and sch.running),
                "running": sch.running.kind if sch and sch.running else None, "queued": sch.queued if sch else 0,
                "gpu_busy": self.gpu_busy_reason(), "idle_s": round(time.time() - self.worker.last_active, 1),
                "last_error": self.last_error}

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kind in ("live", "refine"):
            rows = [r for r in self.recent if r["kind"] == kind]
            if not rows:
                continue
            out[kind] = {"count": len(rows), "median_total_ms": round(median(r["total_ms"] for r in rows), 1),
                         "median_infer_ms": round(median(r["infer_ms"] for r in rows), 1),
                         "max_peak_vram_mb": max(r.get("peak_vram_mb") or 0 for r in rows)}
        return out

    def status(self, query: bool = True) -> dict[str, Any]:
        g = (hardware.gpus() or [{}])[0]
        return {
            **self.summary(),
            "components": C.describe(self.models_dir), "install": dict(self.installer.state),
            "runtime": self.runtime(query), "stats": self.stats(),
            "gpu": {k: g.get(k) for k in ("name", "vram_total_mb", "vram_used_mb", "vram_free_mb", "utilization_pct")},
            "materials": M.public_list(),
            "canvas": {"sizes": CANVAS_SIZES, "min_side": MK.MIN_SIDE, "max_side": MK.MAX_SIDE, "multiple": MK.MULTIPLE},
            "refine": {"scales": REFINE_SCALES, "max_pixels": MAX_REFINE_PIXELS, "quality": QUALITY_STEPS,
                       "defaults": {"quality": "standard", "creativity": DEFAULT_CREATIVITY, "control": 1.0,
                                    "scale": 1.0}},
            "models": {"live": {"id": "spade-landscapes-512", "name": "GauGAN (SPADE) landscapes", "labels": "COCO-Stuff (182)"},
                       "refine": {"id": "sd15-controlnet-seg", "name": "Stable Diffusion 1.5 + ControlNet-Seg 1.1",
                                  "labels": "ADE20K (150)"}},
        }

    def _publish_status(self) -> None:
        self.st.bus.publish("experiment.status", {"experiment": self.id, "runtime": self.runtime(query=False)})

    # ------------------------------------------------------------- events
    def _on_worker_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "progress":
            self.st.bus.publish("experiment.progress", {"experiment": self.id, "render_id": ev["req_id"],
                                                        "session": self._tickets.get(ev["req_id"]),
                                                        "step": ev.get("step"), "total": ev.get("total")})
            return
        if t in ("stopped", "crashed"):
            self.loaded = {"live": False, "refine": False}
        if t == "crashed":
            self.last_error = f"The paintbrush worker crashed (exit code {ev.get('exit_code')}); it restarts on the next request."
        self._publish_status()

    def _on_install(self, state: dict[str, Any]) -> None:
        self.st.bus.publish("experiment.install", {"experiment": self.id, **state})

    # ------------------------------------------------------------ execution
    async def _execute(self, t: Ticket) -> dict[str, Any]:
        reason = await self.gpu_busy_reason_async()
        if reason:
            raise GpuBusy(reason)
        self._tickets[t.id] = t.session
        try:
            for attempt in (0, 1):
                try:
                    fut = await asyncio.to_thread(self.worker.submit, t.kind, t.payload, t.id)
                    res = await asyncio.wrap_future(fut)
                    self.loaded["live"] = True
                    if t.kind == "refine":
                        self.loaded["refine"] = True
                    self.last_error = None
                    return res
                except WorkerError as exc:
                    if exc.code in ("vram", "oom") and attempt == 0 and not t.cancel_reason and await self._free_vram():
                        continue
                    if exc.code == "oom":
                        self.loaded["refine"] = False
                    raise
        finally:
            self._tickets.pop(t.id, None)
        raise AssertionError("unreachable")  # pragma: no cover

    def _cancel_running(self, t: Ticket) -> None:
        self.worker.cancel(t.id)

    async def _free_vram(self) -> bool:
        """Frees VRAM held by idle Sarness components and idle chat models. Returns whether anything was freed."""
        freed = False
        jobs, llama, gw = self.st.jobs, self.st.llama, self.st.gateway
        if jobs.supervisor.alive and not jobs.supervisor.busy:
            await asyncio.to_thread(jobs.supervisor.stop)
            self.st.bus.publish("worker.status", {"loaded": [], "stopped": True})
            freed = True
        if llama.status()["state"] in ("ready", "loading") and not llama.busy:
            await asyncio.to_thread(llama.unload)
            self.st.bus.publish("llm.status", llama.status())
            freed = True
        if gw.configured and not llama.active_streams:
            try:
                unloaded = await gw.unload_llm()
                if unloaded:
                    log.info("asked the gateway to unload idle chat models %s for the paintbrush", unloaded)
                    freed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("gateway unload failed: %s", exc)
        return freed

    # --------------------------------------------------------------- render
    async def render(self, req: dict[str, Any]) -> dict[str, Any]:
        if self.scheduler is None:
            raise PaintbrushError("the paintbrush service is not running", 503)
        avail = self.availability()
        kind = req.get("kind", "live")
        if kind not in ("live", "refine"):
            raise PaintbrushError("kind must be 'live' or 'refine'")
        if not avail["live"]:
            raise PaintbrushError("God's Paintbrush needs its GauGAN model on the server: install it from the "
                                  "experiment's model panel.", 409)
        if kind == "refine" and not avail["refine"]:
            raise PaintbrushError("Refine needs Stable Diffusion 1.5 and ControlNet-Seg on the server; install them "
                                  "from the experiment's model panel.", 409)
        session = str(req.get("session") or "default")[:64]
        labels = MK.decode(req.get("mask"))
        h, w = labels.shape
        meta: dict[str, Any] = {"kind": kind, "width": w, "height": h}
        if kind == "live":
            payload: dict[str, Any] = {"labels": labels, "format": "png" if req.get("final") else "jpeg"}
        else:
            scale = float(req.get("scale", 1.0))
            ow, oh = refine_size(w, h, scale)
            seed = req.get("seed")
            seed = secrets.randbelow(2**31 - 1) if seed is None or int(seed) < 0 else int(seed) % (2**31)
            quality = req.get("quality", "standard")
            if quality not in QUALITY_STEPS:
                raise PaintbrushError(f"quality must be one of {list(QUALITY_STEPS)}")
            creativity = min(1.0, max(0.2, float(req.get("creativity", DEFAULT_CREATIVITY))))
            control = min(1.6, max(0.3, float(req.get("control", 1.0))))
            guidance = str(req.get("prompt") or "")[:500]
            prompt = MK.build_prompt(labels, guidance)
            payload = {"labels": labels, "prompt": prompt, "negative_prompt": MK.NEGATIVE_PROMPT, "seed": seed,
                       "steps": QUALITY_STEPS[quality], "strength": creativity, "control_scale": control,
                       "guidance": 7.0, "width": ow, "height": oh, "format": "png"}
            meta.update(seed=seed, prompt=prompt, guidance=guidance, negative_prompt=MK.NEGATIVE_PROMPT,
                        quality=quality, steps=QUALITY_STEPS[quality], creativity=creativity, control=control,
                        scale=scale)
        t = self.scheduler.submit(session, kind, payload)
        try:
            res = await t.future
        except asyncio.CancelledError:
            self.scheduler.cancel_ticket(t)
            raise
        except RequestCancelled:
            busy = self.gpu_busy_reason()
            if busy:
                raise GpuBusy(busy) from None
            raise
        except WorkerError as exc:
            self.last_error = str(exc)
            if exc.code in ("vram", "oom"):
                raise PaintbrushError(f"Not enough free GPU memory: {exc}", 503) from None
            raise PaintbrushError(f"Rendering failed: {exc}", 500) from None
        total_ms = round((time.perf_counter() - t.created) * 1000, 1)
        timings = {**res.get("timings", {}), "queue_ms": round(((t.started or t.created) - t.created) * 1000, 1),
                   "total_ms": total_ms}
        infer = timings.get("live_ms", 0) + timings.get("refine_ms", 0)
        self.recent.append({"kind": kind, "w": w, "h": h, "total_ms": total_ms, "infer_ms": infer,
                            "peak_vram_mb": res.get("peak_vram_mb"), "at": time.time()})
        fmt = res["format"]
        mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]
        record = {"id": t.id, "session": session, "image": res["image"], "format": fmt, "mime": mime,
                  "labels": labels, "created_at": now(), "timings": timings, "peak_vram_mb": res.get("peak_vram_mb"),
                  **meta, "width": res["width"], "height": res["height"], "mask_width": w, "mask_height": h}
        self.renders[t.id] = record
        while len(self.renders) > RENDER_CACHE:
            self.renders.popitem(last=False)
        return {"render_id": t.id, "session": session, **{k: v for k, v in meta.items() if k not in ("width", "height")},
                "width": res["width"], "height": res["height"], "mime": mime,
                "image": f"data:{mime};base64," + base64.b64encode(res["image"]).decode(),
                "timings": timings, "peak_vram_mb": res.get("peak_vram_mb"), "coverage": MK.coverage(labels),
                "model": "spade-landscapes-512" if kind == "live" else "spade-landscapes-512+sd15-controlnet-seg"}

    def cancel(self, session: str) -> int:
        return self.scheduler.cancel(str(session)[:64]) if self.scheduler else 0

    async def warmup(self, refine: bool = False) -> dict[str, Any]:
        avail = self.availability()
        if not avail["live"] or (refine and not avail["refine"]):
            raise PaintbrushError("the models are not installed", 409)
        reason = await self.gpu_busy_reason_async()
        if reason:
            raise GpuBusy(reason)
        t0 = time.perf_counter()
        for attempt in (0, 1):
            try:
                fut = await asyncio.to_thread(self.worker.submit, "warmup", {"refine": refine})
                await asyncio.wrap_future(fut)
                break
            except WorkerError as exc:
                if exc.code in ("vram", "oom") and attempt == 0 and await self._free_vram():
                    continue
                raise PaintbrushError(f"Warm-up failed: {exc}", 503 if exc.code in ("vram", "oom") else 500) from None
        self.loaded["live"] = True
        self.loaded["refine"] = self.loaded["refine"] or refine
        self._publish_status()
        return {"elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "runtime": self.runtime()}

    async def release(self, level: str) -> dict[str, Any]:
        if level == "refine":
            self.worker.release("refine")
            self.loaded["refine"] = False
        elif level == "all":
            if self.scheduler:
                self.scheduler.cancel_all()
            await asyncio.to_thread(self.worker.stop)
        else:
            raise PaintbrushError("level must be 'refine' or 'all'")
        self._publish_status()
        return self.runtime()

    async def _housekeeping(self) -> None:
        """Idle unloading, and standing down when the gateway starts a GPU job for someone else."""
        while True:
            await asyncio.sleep(3)
            try:
                if not self.worker.alive:
                    continue
                busy = bool(self.scheduler and (self.scheduler.running or self.scheduler.queued))
                gw = self.st.gateway
                if gw.configured and await gw.busy_job() and not busy:
                    await self.yield_gpu("the AI gateway started a GPU job")
                    self._publish_status()
                    continue
                if busy:
                    continue
                idle = time.time() - self.worker.last_active
                refine_idle = float(self.st.settings.get("experiments_refine_idle_minutes") or 0) * 60
                worker_idle = float(self.st.settings.get("experiments_worker_idle_minutes") or 0) * 60
                if worker_idle and idle > worker_idle:
                    log.info("paintbrush idle for %.0fs; stopping its worker to free VRAM", idle)
                    await asyncio.to_thread(self.worker.stop)
                elif refine_idle and idle > refine_idle and self.loaded["refine"]:
                    log.info("paintbrush idle for %.0fs; releasing the refine models", idle)
                    self.worker.release("refine")
                    self.loaded["refine"] = False
                    self._publish_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("paintbrush housekeeping")

    # ------------------------------------------------------------- outputs
    def _render(self, render_id: str) -> dict[str, Any]:
        r = self.renders.get(render_id)
        if not r:
            raise PaintbrushError("this render is no longer available on the server; generate it again", 404)
        return r

    def _export_path(self, ext: str) -> Path:
        t = time.localtime()
        d = self.st.cfg.outputs_dir / "experiments" / EXPERIMENT_ID / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"paintbrush-{time.strftime('%Y%m%d-%H%M%S', t)}-{new_id()[:8]}.{ext}"

    def export_render(self, render_id: str) -> dict[str, Any]:
        r = self._render(render_id)
        dest = self._export_path("jpg" if r["format"] == "jpeg" else r["format"])
        dest.write_bytes(r["image"])
        f = self.st.storage.register(dest, "export", mime=r["mime"], width=r["width"], height=r["height"],
                                     original_name=dest.name)
        return {"file_id": f["id"], "url": self.st.storage.file_url(f["id"]), "mime": r["mime"], "name": dest.name}

    def export_mask(self, mask: dict[str, Any], fmt: str) -> dict[str, Any]:
        labels = MK.decode(mask)
        data = MK.export_png(labels, fmt)
        dest = self._export_path("png")
        dest.write_bytes(data)
        h, w = labels.shape
        f = self.st.storage.register(dest, "export", mime="image/png", width=w, height=h, original_name=dest.name)
        legend = []
        for idx in sorted({int(i) for i in set(labels.ravel().tolist())}):
            m = M.by_index(idx)
            value: Any = {"coco": m.coco_id, "index": idx, "ade": "#%02x%02x%02x" % M.ADE_COLORS[m.ade_index],
                          "color": m.color}[fmt]
            legend.append({"material": m.id, "label": m.label, "value": value})
        return {"file_id": f["id"], "url": self.st.storage.file_url(f["id"]), "format": fmt, "legend": legend,
                "name": dest.name}

    def save_render(self, render_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Adds a render to the Sarness Library as an image generation (with its mask for lineage)."""
        r = self._render(render_id)
        st = self.st
        ext = "jpg" if r["format"] == "jpeg" else r["format"]
        dest = st.storage.output_path("image", ext)
        dest.write_bytes(r["image"])
        f = st.storage.register(dest, "image", mime=r["mime"], width=r["width"], height=r["height"])
        thumb = st.storage.make_image_thumb(dest)
        mask_dest = st.cfg.uploads_dir / f"{new_id('u_')}.png"
        mask_dest.write_bytes(MK.export_png(r["labels"], "coco"))
        mf = st.storage.register(mask_dest, "upload", mime="image/png", width=r["mask_width"], height=r["mask_height"],
                                 original_name="paintbrush-mask-coco-stuff.png")
        model_id = (f"experiment:{EXPERIMENT_ID}/spade-landscapes-512" if r["kind"] == "live"
                    else f"experiment:{EXPERIMENT_ID}/sd15-controlnet-seg")
        prompt = r.get("prompt") or "God's Paintbrush: " + ", ".join(
            M.MATERIALS[M.INDEX[k] - 1].label.lower() for k in MK.coverage(r["labels"]))
        metadata = {"experiment": EXPERIMENT_ID, "stage": r["kind"], "backend": "experiment",
                    "models": ["spade-landscapes-512"] + (["sd15", "controlnet-seg"] if r["kind"] == "refine" else []),
                    "mask_file_id": mf["id"], "mask_format": "COCO-Stuff class ids (8-bit grey)",
                    "coverage": MK.coverage(r["labels"]), "document_id": body.get("document_id"),
                    "timings": r["timings"], "peak_vram_mb": r.get("peak_vram_mb"), "engine_version": __version__,
                    **{k: r[k] for k in ("guidance", "quality", "steps", "creativity", "control", "scale") if k in r}}
        gen = {"id": new_id("g_"), "job_id": None, "type": "image", "project_id": body.get("project_id"),
               "folder_id": body.get("folder_id"), "file_id": f["id"], "thumb_file_id": thumb["id"],
               "prompt": prompt, "negative_prompt": r.get("negative_prompt"), "model_id": model_id,
               "seed": r.get("seed"), "width": r["width"], "height": r["height"], "frames": None, "fps": None,
               "duration": None, "metadata": metadata, "workflow": None, "simple": None, "favorite": 0,
               "created_at": now()}
        st.db.insert("generations", gen)
        st.db.execute("INSERT OR IGNORE INTO tags(generation_id, tag) VALUES(?, ?)", [gen["id"], "gods-paintbrush"])
        pub = st.jobs.generation_public(gen)
        st.bus.publish("generation.created", pub)
        return pub
