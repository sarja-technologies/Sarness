"""Generation through an existing AI gateway that already owns the GPU.

Some servers already run their own image/video service (one GPU queue, its own
model copies, an API token). Loading a second copy of those models inside
Sarness would make the two processes fight over the same VRAM, so instead the
gateway's models are exposed as *remote* models: Sarness keeps the queue,
library, lineage and UI, and hands the actual inference to the gateway.

Gateway protocol (``ENDPOINTS.md`` on the server):

* ``GET  /models``                      inventory (``image`` / ``video`` lists)
* ``POST /uploads``                     multipart ``file`` → ``upload_id``
* ``POST /jobs/image`` | ``/jobs/video``  → ``job_id``
* ``GET  /jobs/{id}``                   status, ``progress {stage, step, total}``, ``result``, ``error``
* ``POST /jobs/{id}/cancel``
* ``GET  /jobs/{id}/output``            PNG / MP4

The bearer token is stored as a secret setting and never returned by the API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ..workflow.graph import clamp_seed

log = logging.getLogger("sarness.gateway")

FAMILY = "gateway"
PREFIX = "gw:"

# Honest per-model profiles for the models known to ship with the gateway.
# Unknown models get a conservative generic profile.
PROFILES: dict[str, dict[str, Any]] = {
    "z-image-turbo": {
        "name": "Z-Image Turbo",
        "description": "Fast 9-step text-to-image (runs on the server's AI gateway).",
        "defaults": {"steps": 9, "guidance": 0.0, "width": 1024, "height": 1024},
        "limits": {"steps": [1, 50], "min_side": 256, "max_side": 2048, "multiple": 16},
        "capabilities": {"modes": ["txt2img"], "negative_prompt": False, "lora": False, "guidance": False},
    },
    "qwen-image-2.1-turbo": {
        "name": "Qwen-Image 2.1 Turbo",
        "description": "Text-to-image and reference-image editing with a fixed 8-step schedule (server AI gateway).",
        "defaults": {"steps": 8, "guidance": 0.0, "width": 1024, "height": 1024},
        "limits": {"steps": [8, 8], "min_side": 256, "max_side": 2048, "multiple": 16},
        "capabilities": {"modes": ["txt2img", "img2img"], "negative_prompt": False, "lora": False,
                         "guidance": False, "steps": False, "denoise": False},
    },
    "wan2.2-ti2v-5b": {
        "name": "Wan 2.2 TI2V 5B",
        "description": "Text- and image-to-video at 480p/24 fps (server AI gateway). About 6 minutes per 33-frame clip.",
        "defaults": {"steps": 20, "guidance": 5.0, "width": 832, "height": 480, "frames": 33, "fps": 24},
        "limits": {"steps": [1, 50], "frames": [5, 121], "frame_step": 4, "fps": [24, 24],
                   "resolutions": [[832, 480], [480, 832]], "multiple": 16},
        "capabilities": {"modes": ["t2v", "i2v"], "negative_prompt": False, "start_frame": True, "end_frame": False,
                         "motion": [], "lora": False, "guidance": False, "flow_shift": False},
        "time_estimate_s": 400,
    },
}


class GatewayError(Exception):
    pass


class Gateway:
    def __init__(self, settings):
        self.settings = settings
        self.last_error: str | None = None
        self.last_models: list[dict[str, Any]] = []
        self._health: tuple[float, dict[str, Any]] = (0.0, {})

    # ------------------------------------------------------------- config
    @property
    def base_url(self) -> str:
        return (self.settings.get("gateway_url") or "").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        tok = self.settings.get("gateway_token") or ""
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers(), timeout=timeout)

    # ------------------------------------------------------------- models
    def refresh_sync(self) -> list[dict[str, Any]]:
        """Fetches the gateway inventory and returns registry rows (never raises)."""
        if not self.configured:
            self.last_models, self.last_error = [], None
            return []
        try:
            with httpx.Client(base_url=self.base_url, headers=self._headers(), timeout=10) as c:
                r = c.get("/models")
                if r.status_code == 401:
                    raise GatewayError("the gateway rejected the API token")
                r.raise_for_status()
                inv = r.json()
        except Exception as exc:
            self.last_error = str(exc) or exc.__class__.__name__
            log.warning("gateway inventory failed: %s", self.last_error)
            return self.last_models  # keep the previous view while the gateway is briefly unavailable
        rows = []
        for category in ("image", "video"):
            for m in inv.get(category) or []:
                if "INSTALLED" not in str(m.get("status", "INSTALLED")).upper():
                    continue
                rows.append(self._row(category, m))
        self.last_models, self.last_error = rows, None
        return rows

    def _row(self, category: str, m: dict[str, Any]) -> dict[str, Any]:
        gid = m["id"]
        prof = PROFILES.get(gid) or _generic_profile(category, m)
        caps = {**prof["capabilities"], "remote": True}
        return {
            "id": PREFIX + gid, "catalog_id": None, "name": prof.get("name") or gid, "category": category,
            "family": FAMILY, "format": "remote", "path": self.base_url, "size": None,
            "source": {"gateway": self.base_url, "gateway_id": gid},
            "meta": {"remote": True, "gateway_id": gid, "status": m.get("status"),
                     "profile": {"description": prof.get("description"), "defaults": prof.get("defaults", {}),
                                 "limits": prof.get("limits", {}), "capabilities": caps,
                                 "vram_mb": None, "vram_offload_mb": None,
                                 "tags": ["server-gateway"]}},
        }

    # ------------------------------------------------------------ GPU sharing
    async def health(self, max_age: float = 2.0) -> dict[str, Any]:
        """Cached ``GET /health`` (no auth); ``{}`` when unreachable."""
        ts, data = self._health
        if not self.configured or time.time() - ts < max_age:
            return data if self.configured else {}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=3) as c:
                r = await c.get("/health")
                data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        self._health = (time.time(), data)
        return data

    async def busy_job(self) -> str | None:
        """Id of the GPU job the gateway is running (for any client), if any."""
        return (await self.health()).get("gpu_worker_current_job")

    async def unload_llm(self) -> list[str]:
        """Asks the gateway to unload its resident Ollama chat models (frees their VRAM)."""
        async with self._client(timeout=90) as c:
            r = await c.post("/models/unload")
            _check(r)
            return list(r.json().get("unloaded") or [])

    # -------------------------------------------------------------- jobs
    async def run(self, *, kind: str, gateway_id: str, simple: dict[str, Any], files: dict[str, str],
                  storage, progress, cancelled, set_remote_id) -> list[dict[str, Any]]:
        """Runs one Sarness job (possibly a batch) on the gateway and returns output descriptors."""
        batch = max(1, int(simple.get("batch_size") or 1)) if kind == "image" else 1
        outputs: list[dict[str, Any]] = []
        async with self._client(timeout=60) as c:
            upload_id = None
            ref = simple.get("reference_image") if kind == "image" else simple.get("start_image")
            if ref:
                path = files.get(ref)
                if not path:
                    raise GatewayError("the input image is missing")
                progress(0.0, "Uploading input image")
                with open(path, "rb") as f:
                    r = await c.post("/uploads", files={"file": (Path(path).name, f.read())})
                _check(r)
                upload_id = r.json()["upload_id"]
            seed = int(simple.get("seed") if simple.get("seed") is not None else -1)
            for i in range(batch):
                body: dict[str, Any] = {"model": gateway_id, "prompt": simple.get("prompt") or "",
                                        "width": int(simple["width"]), "height": int(simple["height"]),
                                        "steps": int(simple["steps"])}
                if seed >= 0:
                    body["seed"] = clamp_seed(seed + i)
                if kind == "video":
                    body["frames"] = int(simple.get("frames") or 33)
                if upload_id:
                    body["image_upload_id"] = upload_id
                r = await c.post(f"/jobs/{kind}", json=body)
                _check(r)
                rid = r.json()["job_id"]
                set_remote_id(rid)
                result = await self._wait(c, rid, i, batch, progress, cancelled)
                if result is None:
                    return []  # cancelled
                ext = "png" if kind == "image" else "mp4"
                dest = storage.output_path(kind, ext)
                tmp = dest.with_suffix(dest.suffix + ".part")
                async with c.stream("GET", f"/jobs/{rid}/output", timeout=300) as resp:
                    _check(resp)
                    with open(tmp, "wb") as f:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            f.write(chunk)
                tmp.rename(dest)
                outputs.append(self._describe(kind, dest, result, simple, gateway_id, i))
        return outputs

    async def _wait(self, c: httpx.AsyncClient, rid: str, index: int, batch: int, progress, cancelled):
        cancel_sent = False
        last = None
        t0 = time.time()
        while True:
            if cancelled() and not cancel_sent:
                cancel_sent = True
                try:
                    await c.post(f"/jobs/{rid}/cancel")
                except Exception as exc:
                    log.warning("gateway cancel failed for %s: %s", rid, exc)
            try:
                r = await c.get(f"/jobs/{rid}")
                _check(r)
                j = r.json()
            except (httpx.TransportError, GatewayError) as exc:
                if time.time() - t0 > 30 and last is None:
                    raise
                log.warning("gateway poll failed (%s); retrying", exc)
                await asyncio.sleep(3)
                continue
            last = j
            status = j.get("status")
            if status == "succeeded":
                return j.get("result") or {}
            if status == "cancelled":
                return None
            if status == "failed":
                err = j.get("error") or "the gateway job failed"
                tail = (j.get("log_tail") or "").strip().splitlines()[-3:]
                raise GatewayError(f"{err}" + (f" — {' | '.join(tail)}" if tail else ""))
            p = j.get("progress") or {}
            stage, step, total = p.get("stage"), p.get("step"), p.get("total")
            frac = (step / total) if step and total else 0.0
            if stage == "saving":
                frac = 0.98
            text = {"loading": "Loading model on the server", "denoising": f"Denoising {step}/{total}" if total else "Denoising",
                    "saving": "Saving"}.get(stage or "", "Waiting for the server GPU" if status == "queued" else "Running")
            if batch > 1:
                text = f"Image {index + 1}/{batch} · {text}"
            progress((index + min(frac, 0.99)) / batch, text, step, total)
            await asyncio.sleep(1.0)

    @staticmethod
    def _describe(kind: str, path: Path, res: dict[str, Any], simple: dict[str, Any], gid: str, index: int) -> dict[str, Any]:
        lineage = {"prompt": simple.get("prompt"), "negative_prompt": None, "model_id": PREFIX + gid,
                   "seed": res.get("seed"), "steps": res.get("steps"), "backend": "gateway",
                   "gateway_model": res.get("model") or gid, "mode": res.get("mode"),
                   "load_s": res.get("load_s"), "gen_s": res.get("gen_s"), "peak_vram_gb": res.get("peak_vram_gb")}
        out = {"path": str(path), "kind": kind, "width": res.get("width") or simple.get("width"),
               "height": res.get("height") or simple.get("height"), "batch_index": index, "node_id": "gateway",
               "lineage": {k: v for k, v in lineage.items() if v is not None}}
        if kind == "image":
            out["mime"] = "image/png"
        else:
            out.update(mime="video/mp4", frames=res.get("frames"), fps=res.get("fps"), duration=res.get("duration_s"))
        return out


def _check(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    if r.status_code == 401:
        raise GatewayError("the gateway rejected the API token")
    try:
        detail = r.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) for d in detail)
    raise GatewayError(f"gateway error {r.status_code}: {detail or r.reason_phrase}")


def _generic_profile(category: str, m: dict[str, Any]) -> dict[str, Any]:
    if category == "image":
        return {"defaults": {"steps": int(m.get("default_steps") or 20), "guidance": 0.0, "width": 1024, "height": 1024},
                "limits": {"steps": [1, 50], "min_side": 256, "max_side": 2048, "multiple": 16},
                "capabilities": {"modes": ["txt2img"], "negative_prompt": False, "lora": False, "guidance": False}}
    sizes = [[int(a) for a in s.split("x")] for s in (m.get("allowed_sizes") or ["832x480"])]
    return {"defaults": {"steps": int(m.get("default_steps") or 20), "guidance": 5.0, "width": sizes[0][0],
                         "height": sizes[0][1], "frames": 33, "fps": 24},
            "limits": {"steps": [1, 50], "frames": [5, int(m.get("max_frames") or 81)], "frame_step": 4,
                       "fps": [24, 24], "resolutions": sizes, "multiple": 16},
            "capabilities": {"modes": ["t2v"], "negative_prompt": False, "start_frame": False, "end_frame": False,
                             "motion": [], "lora": False, "guidance": False, "flow_shift": False}}
