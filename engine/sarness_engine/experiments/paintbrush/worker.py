"""God's Paintbrush GPU handler (runs inside the experiment worker process).

Two stages, both conditioned on the same semantic label map:

* ``live``   — SPADE landscapes generator: label map → photo in one forward pass.
* ``refine`` — the SPADE frame upscaled to the output size is re-rendered by
  Stable Diffusion 1.5 img2img under ControlNet-Seg control (ADE20K colours of
  the same label map), with a seed and optional text guidance. ``strength`` 1.0
  ignores the SPADE frame entirely (pure ControlNet-Seg generation).

Models load lazily: the live model on first use (~1 s, ~1.3 GB), the refine
pipeline on the first refine (~3 GB). ``release("refine")`` drops only the
refine pipeline; ``release("all")`` drops both.
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any, Callable

import numpy as np
from PIL import Image

from . import masks as MK

log = logging.getLogger("sarness.experiments.paintbrush.worker")

# Measured on an RTX 4070 Ti (see docs/EXPERIMENTS.md); used to refuse loads that cannot fit.
LIVE_LOAD_MB = 1200
REFINE_LOAD_MB = 3600


class Cancelled(Exception):
    pass


class NeedVram(Exception):
    def __init__(self, need_mb: int, free_mb: int, what: str):
        super().__init__(f"{what} needs about {need_mb} MiB of free GPU memory; {free_mb} MiB are free")
        self.need_mb, self.free_mb, self.what = need_mb, free_mb, what


class PaintbrushHandler:
    def __init__(self, options: dict[str, Any], emit: Callable[[dict[str, Any]], None]):
        import torch

        self.torch = torch
        self.paths: dict[str, str] = options["paths"]
        self.emit = emit
        self.cuda = torch.cuda.is_available()
        self.device = "cuda" if self.cuda else "cpu"
        self.dtype = torch.float16 if self.cuda else torch.float32
        # Autotuning re-runs for every new canvas/output shape: measured 10.5 s for the first live frame and 22 s for
        # the first refine on a 4070 Ti, against 0.34 s / 1.3 s without it, for a ~2 % steady-state gain.
        torch.backends.cudnn.benchmark = False
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        self.spade = None
        self.pipe = None
        self.loaded_at: dict[str, float] = {}
        self.load_s: dict[str, float] = {}

    # ------------------------------------------------------------ lifecycle
    def _free_mb(self) -> int:
        if not self.cuda:
            return 1 << 30
        return int(self.torch.cuda.mem_get_info()[0] // 2**20)

    def _ensure_live(self) -> None:
        if self.spade is not None:
            return
        free = self._free_mb()
        if free < LIVE_LOAD_MB:
            raise NeedVram(LIVE_LOAD_MB, free, "The live model")
        from .spade import load_generator

        t0 = time.perf_counter()
        self.spade = load_generator(os.path.join(self.paths["spade-landscapes-512"], "latest_net_G.pth"),
                                    self.device, self.dtype)
        self.load_s["live"] = round(time.perf_counter() - t0, 2)
        self.loaded_at["live"] = time.time()
        log.info("live model loaded in %.2fs", self.load_s["live"])

    def _ensure_refine(self) -> None:
        if self.pipe is not None:
            return
        free = self._free_mb()
        if free < REFINE_LOAD_MB:
            raise NeedVram(REFINE_LOAD_MB, free, "The refine model")
        from diffusers import ControlNetModel, StableDiffusionControlNetImg2ImgPipeline, UniPCMultistepScheduler

        from ...runtime.models import fast_load, load_pipeline

        t0 = time.perf_counter()
        cn = ControlNetModel.from_pretrained(self.paths["controlnet-seg"], dtype=self.dtype, variant="fp16",
                                             use_safetensors=True, **fast_load())
        pipe = load_pipeline(
            StableDiffusionControlNetImg2ImgPipeline, self.paths["sd15"], self.dtype, controlnet=cn, variant="fp16",
            use_safetensors=True, safety_checker=None, feature_extractor=None, requires_safety_checker=False)
        pipe.scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config)
        pipe.to(self.device)
        pipe.unet.to(memory_format=self.torch.channels_last)
        pipe.controlnet.to(memory_format=self.torch.channels_last)
        pipe.set_progress_bar_config(disable=True)
        self.pipe = pipe
        self.load_s["refine"] = round(time.perf_counter() - t0, 2)
        self.loaded_at["refine"] = time.time()
        log.info("refine pipeline loaded in %.2fs", self.load_s["refine"])

    def release(self, level: str = "all") -> None:
        if level in ("refine", "all") and self.pipe is not None:
            self.pipe = None
            self.loaded_at.pop("refine", None)
        if level == "all" and self.spade is not None:
            self.spade = None
            self.loaded_at.pop("live", None)
        import gc

        gc.collect()
        if self.cuda:
            self.torch.cuda.empty_cache()

    def on_oom(self) -> None:
        self.release("refine")  # the live model is small; keep it so painting continues

    def status(self) -> dict[str, Any]:
        t = self.torch
        return {"device": t.cuda.get_device_name(0) if self.cuda else "cpu",
                "loaded": {"live": self.spade is not None, "refine": self.pipe is not None},
                "load_s": dict(self.load_s),
                "vram_allocated_mb": int(t.cuda.memory_allocated() // 2**20) if self.cuda else 0,
                "vram_reserved_mb": int(t.cuda.memory_reserved() // 2**20) if self.cuda else 0}

    # ------------------------------------------------------------- stages
    def _spade_frame(self, labels: np.ndarray) -> np.ndarray:
        t = self.torch
        from .spade import one_hot

        coco = t.from_numpy(MK.to_coco(labels).astype(np.int64)).to(self.device)
        with t.inference_mode():
            y = self.spade(one_hot(coco, self.dtype))
            img = ((y[0].float().clamp(-1, 1) + 1) * 127.5).round().to(t.uint8).permute(1, 2, 0)
        return img.cpu().numpy()

    def _sync(self) -> None:
        if self.cuda:
            self.torch.cuda.synchronize()

    def run(self, op: str, p: dict[str, Any], cancelled: Callable[[], bool],
            progress: Callable[[int, int], None]) -> dict[str, Any]:
        t = self.torch
        if self.cuda:
            t.cuda.reset_peak_memory_stats()
        if op == "warmup":
            self._ensure_live()
            dummy = np.full((512, 768), 1, np.uint8)
            dummy[256:] = 7
            self._spade_frame(dummy)
            if p.get("refine"):
                self._ensure_refine()
            self._sync()
            return {"status": self.status()}
        labels: np.ndarray = p["labels"]
        h, w = labels.shape
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        load0 = dict(self.load_s)
        self._ensure_live()
        if op == "refine":
            self._ensure_refine()
        timings["load_ms"] = round((time.perf_counter() - t0) * 1000, 1) if self.load_s != load0 else 0.0
        t1 = time.perf_counter()
        frame = self._spade_frame(labels)
        self._sync()
        timings["live_ms"] = round((time.perf_counter() - t1) * 1000, 1)
        if op == "live":
            fmt = p.get("format", "jpeg")
            data = _encode(Image.fromarray(frame), fmt)
            return {"image": data, "format": fmt, "width": w, "height": h, "timings": timings,
                    "peak_vram_mb": int(t.cuda.max_memory_allocated() // 2**20) if self.cuda else 0}
        if op != "refine":
            raise ValueError(f"unknown operation {op}")
        if cancelled():
            raise Cancelled()
        ow, oh = int(p["width"]), int(p["height"])
        init = Image.fromarray(frame)
        if (ow, oh) != (w, h):
            init = init.resize((ow, oh), Image.LANCZOS)
        control = Image.fromarray(MK.to_ade_rgb(labels))
        if (ow, oh) != (w, h):
            control = control.resize((ow, oh), Image.NEAREST)
        steps = int(p.get("steps", 20))
        strength = float(p.get("strength", 0.55))
        effective = max(1, int(steps * strength))
        large = ow * oh > 1024 * 1024
        if large:
            self.pipe.vae.enable_tiling()
        else:
            self.pipe.vae.disable_tiling()

        def on_step(pipe, i, timestep, kwargs):
            if cancelled():
                raise Cancelled()
            progress(i + 1, effective)
            return kwargs

        gen = t.Generator(self.device).manual_seed(int(p["seed"]))
        t2 = time.perf_counter()
        out = self.pipe(prompt=p["prompt"], negative_prompt=p.get("negative_prompt") or MK.NEGATIVE_PROMPT,
                        image=init, control_image=control, strength=strength, num_inference_steps=steps,
                        guidance_scale=float(p.get("guidance", 7.0)),
                        controlnet_conditioning_scale=float(p.get("control_scale", 1.0)),
                        width=ow, height=oh, generator=gen, callback_on_step_end=on_step,
                        output_type="np").images[0]
        self._sync()
        timings["refine_ms"] = round((time.perf_counter() - t2) * 1000, 1)
        if not np.isfinite(out).all():
            raise RuntimeError("the refine model produced NaNs; try a different seed")
        img = Image.fromarray((np.clip(out, 0, 1) * 255).round().astype(np.uint8))
        fmt = p.get("format", "png")
        return {"image": _encode(img, fmt), "format": fmt, "width": ow, "height": oh, "timings": timings,
                "steps": steps, "effective_steps": effective,
                "peak_vram_mb": int(t.cuda.max_memory_allocated() // 2**20) if self.cuda else 0}


def _encode(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.save(buf, "JPEG", quality=90)
    elif fmt == "webp":
        img.save(buf, "WEBP", quality=92)
    else:
        img.save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def errors() -> dict[type, str]:
    """Exception → wire error code (read by the generic worker loop)."""
    return {Cancelled: "cancelled", NeedVram: "vram"}
