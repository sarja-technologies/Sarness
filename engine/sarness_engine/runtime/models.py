"""GPU-worker model manager: loads, places, caches and evicts diffusion models.

Runs only inside the GPU worker process. Placement policy ("memory mode"):

* ``gpu``        — every component resident in VRAM (fastest)
* ``offload``    — diffusers model CPU offload: components move to the GPU only
                   while they run (moderate speed, much lower peak VRAM)
* ``sequential`` — layer-by-layer offload (slowest, minimal VRAM)
* ``auto``       — ``gpu`` when the catalog estimate fits the free VRAM after
                   evicting other cached models, otherwise ``offload``
"""

from __future__ import annotations

import gc
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import torch

log = logging.getLogger("sarness.models")

SCHEDULERS: dict[str, tuple[str, dict[str, Any]]] = {
    "euler_a": ("EulerAncestralDiscreteScheduler", {}),
    "euler": ("EulerDiscreteScheduler", {}),
    "dpmpp_2m": ("DPMSolverMultistepScheduler", {}),
    "dpmpp_2m_karras": ("DPMSolverMultistepScheduler", {"use_karras_sigmas": True}),
    "dpmpp_sde_karras": ("DPMSolverSinglestepScheduler", {"use_karras_sigmas": True}),
    "ddim": ("DDIMScheduler", {}),
    "unipc": ("UniPCMultistepScheduler", {}),
    "lcm": ("LCMScheduler", {}),
    "heun": ("HeunDiscreteScheduler", {}),
    "turbo": ("EulerAncestralDiscreteScheduler", {"timestep_spacing": "trailing"}),
}


@dataclass(frozen=True)
class LoraRef:
    model_id: str
    path: str
    weight: float


@dataclass
class ModelHandle:
    """Value carried on MODEL / VIDEO_MODEL sockets."""

    key: str
    model_id: str
    family: str
    info: dict[str, Any]
    pipe: Any
    memory_mode: str
    loras: tuple[LoraRef, ...] = ()
    vae: Any = None  # optional VAE override

    def with_lora(self, ref: LoraRef) -> "ModelHandle":
        return replace(self, loras=self.loras + (ref,))


@dataclass
class _Loaded:
    obj: Any
    vram_mb: int
    kind: str
    mode: str
    loaded_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)


def cuda_available() -> bool:
    return torch.cuda.is_available()


def device() -> torch.device:
    return torch.device("cuda" if cuda_available() else "cpu")


def free_vram_mb() -> int:
    if not cuda_available():
        return 0
    if unified_memory():
        # cudaMemGetInfo ignores reclaimable page cache; what the GPU can still allocate is MemAvailable.
        import psutil

        return psutil.virtual_memory().available // (1024 * 1024)
    free, _total = torch.cuda.mem_get_info()
    return free // (1024 * 1024)


def total_vram_mb() -> int:
    if not cuda_available():
        return 0
    return torch.cuda.mem_get_info()[1] // (1024 * 1024)


_UNIFIED: bool | None = None


def unified_memory() -> bool:
    """True on GPUs that allocate from system RAM (DGX Spark GB10, Jetson). There, CPU offload
    saves no memory and only slows generation down, and cudaMemGetInfo under-reports what can be
    allocated (it ignores reclaimable page cache), so pipelines always stay resident."""
    global _UNIFIED
    if _UNIFIED is None:
        try:
            from ..hardware import gpus

            g = gpus()
            _UNIFIED = bool(g and g[0].get("unified_memory"))
        except Exception:  # pragma: no cover
            _UNIFIED = False
    return _UNIFIED


def fast_load() -> dict[str, Any]:
    """from_pretrained kwargs that read weights in one pass instead of through mmap. On unified-memory
    GPUs (GB10) moving mmap'd safetensors to the GPU faults pages in one by one on a single CPU core:
    a 14 GB transformer took 79 s that way and 7 s read up front."""
    return {"disable_mmap": True} if unified_memory() else {}


def load_pipeline(cls: Any, path: str | Path, dtype: torch.dtype, **kwargs: Any) -> Any:
    """cls.from_pretrained with fast loading for every component. diffusers forwards disable_mmap only
    to its own models, so transformers components (text encoders) are loaded here and passed in."""
    fast = fast_load()
    path = Path(path)
    if fast and (path / "model_index.json").is_file():
        import transformers

        variant = kwargs.get("variant")
        for name, spec in json.loads((path / "model_index.json").read_text()).items():
            if name in kwargs or not isinstance(spec, list) or len(spec) != 2 or spec[0] != "transformers":
                continue
            mcls = getattr(transformers, spec[1] or "", None)
            if not (isinstance(mcls, type) and issubclass(mcls, transformers.PreTrainedModel)):
                continue  # tokenizers, processors
            v = variant if variant and _has(path / name, f"*.{variant}.safetensors") else None
            kwargs[name] = mcls.from_pretrained(str(path), subfolder=name, dtype=dtype, variant=v, **fast)
    return cls.from_pretrained(str(path), dtype=dtype, **fast, **kwargs)


# Families whose diffusers pipeline is called end to end (prompt → images/frames) instead of
# through Sarness' encode / sample / decode nodes.
NATIVE_PIPELINES = {"qwenimage21": "QwenImage21Pipeline", "ltx2": "LTX2Pipeline"}


def _has(path: Path, pattern: str) -> bool:
    return any(path.glob(pattern))


class ModelManager:
    def __init__(self, headroom_mb: int = 600):
        self._cache: OrderedDict[str, _Loaded] = OrderedDict()
        self.headroom_mb = headroom_mb

    # ---------------------------------------------------------------- status
    def status(self) -> list[dict[str, Any]]:
        return [{"key": k, "kind": v.kind, "mode": v.mode, "vram_mb": v.vram_mb,
                 "loaded_at": v.loaded_at, "last_used": v.last_used} for k, v in self._cache.items()]

    def loaded_keys(self) -> list[str]:
        return list(self._cache)

    # ------------------------------------------------------------- lifecycle
    def _touch(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if item:
            item.last_used = time.time()
            self._cache.move_to_end(key)
            return item.obj
        return None

    def evict(self, key: str) -> None:
        item = self._cache.pop(key, None)
        if item is None:
            return
        log.info("unloading %s", key)
        obj = item.obj[0] if isinstance(item.obj, tuple) else item.obj
        # Move weights off the GPU explicitly: this frees VRAM even if stray
        # references (caches, tracebacks) keep the Python objects alive.
        try:
            if hasattr(obj, "remove_all_hooks"):
                obj.remove_all_hooks()
            if hasattr(obj, "components"):
                for comp in obj.components.values():
                    if isinstance(comp, torch.nn.Module):
                        comp.to("cpu")
                for sub in (obj.__dict__.get("_sarness_denoise") or {}).values():
                    sub.__dict__.pop("_sarness_denoise", None)
            elif hasattr(obj, "model") and isinstance(getattr(obj, "model"), torch.nn.Module):
                obj.model.to("cpu")  # spandrel descriptor
            elif isinstance(obj, torch.nn.Module):
                obj.to("cpu")
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("error while unloading %s: %s", key, exc)
        del obj, item
        self.collect()

    def unload_all(self) -> None:
        for key in list(self._cache):
            self.evict(key)

    @staticmethod
    def collect() -> None:
        gc.collect()
        if cuda_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()

    def make_room(self, need_mb: int, keep: set[str] | None = None) -> None:
        keep = keep or set()
        for key in list(self._cache):
            if free_vram_mb() >= need_mb + self.headroom_mb:
                return
            if key not in keep:
                self.evict(key)

    def _room_before_load(self, need_mb: int, key: str) -> None:
        """With unified memory, a model loads straight into the memory the GPU uses, so cached models
        are evicted first; otherwise the load itself could exhaust memory."""
        if unified_memory():
            self.make_room(need_mb, keep={key})

    def _choose_mode(self, requested: str, need_gpu_mb: int, keep: set[str]) -> str:
        if not cuda_available():
            return "cpu"
        if requested == "auto" and unified_memory():
            self.make_room(need_gpu_mb, keep)
            return "gpu"
        if requested in ("gpu", "offload", "sequential"):
            if requested == "gpu":
                self.make_room(need_gpu_mb, keep)
            return requested
        self.make_room(need_gpu_mb, keep)
        return "gpu" if free_vram_mb() >= need_gpu_mb + self.headroom_mb else "offload"

    @staticmethod
    def _place(pipe: Any, mode: str) -> None:
        if mode == "gpu":
            pipe.to("cuda")
        elif mode == "offload":
            pipe.enable_model_cpu_offload()
        elif mode == "sequential":
            pipe.enable_sequential_cpu_offload()

    # ----------------------------------------------------------- image models
    def image_pipeline(self, info: dict[str, Any], memory_mode: str = "auto") -> ModelHandle:
        family = info.get("family")
        key = f"image:{info['id']}"
        cached = self._touch(key)
        if cached is not None:
            pipe, mode = cached
            return ModelHandle(key, info["id"], family, info, pipe, mode)
        if family in NATIVE_PIPELINES:
            return self._native_pipeline("image", info, memory_mode)

        from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

        path = Path(info["path"])
        dtype = torch.float16 if cuda_available() else torch.float32
        est = int(info.get("vram_mb") or 8000)
        self._room_before_load(est, key)
        t0 = time.perf_counter()
        cls = StableDiffusionXLPipeline if family == "sdxl" else StableDiffusionPipeline
        if path.is_file():
            pipe = cls.from_single_file(str(path), dtype=dtype)
        else:
            variant = "fp16" if _has(path, "unet/*.fp16.safetensors") else None
            pipe = load_pipeline(cls, path, dtype, variant=variant, use_safetensors=True)
        pipe.set_progress_bar_config(disable=True)
        pipe._sarness_default_scheduler = pipe.scheduler
        pipe._sarness_adapters = {}
        mode = self._choose_mode(memory_mode, est, keep={key})
        self._place(pipe, mode)
        log.info("loaded %s (%s) in %.1fs, mode=%s", info["id"], family, time.perf_counter() - t0, mode)
        self._cache[key] = _Loaded((pipe, mode), est if mode == "gpu" else int(info.get("vram_offload_mb") or est // 2), "image", mode)
        return ModelHandle(key, info["id"], family, info, pipe, mode)

    def vae(self, info: dict[str, Any]) -> Any:
        key = f"vae:{info['id']}"
        cached = self._touch(key)
        if cached is not None:
            return cached
        from diffusers import AutoencoderKL

        path = Path(info["path"])
        dtype = torch.float16 if cuda_available() else torch.float32
        vae = (AutoencoderKL.from_single_file(str(path), dtype=dtype) if path.is_file()
               else AutoencoderKL.from_pretrained(str(path), dtype=dtype, **fast_load()))
        vae.to(device())
        self._cache[key] = _Loaded(vae, int(info.get("vram_mb") or 400), "vae", "gpu")
        return vae

    def controlnet(self, info: dict[str, Any]) -> Any:
        key = f"controlnet:{info['id']}"
        cached = self._touch(key)
        if cached is not None:
            return cached
        from diffusers import ControlNetModel

        path = Path(info["path"])
        dtype = torch.float16 if cuda_available() else torch.float32
        variant = "fp16" if _has(path, "*.fp16.safetensors") else None
        cn = ControlNetModel.from_pretrained(str(path), dtype=dtype, variant=variant, use_safetensors=True,
                                             **fast_load())
        self._cache[key] = _Loaded(cn, int(info.get("vram_mb") or 2500), "controlnet", "lazy")
        return cn

    def upscaler(self, info: dict[str, Any]) -> Any:
        key = f"upscaler:{info['id']}"
        cached = self._touch(key)
        if cached is not None:
            return cached
        import spandrel

        path = Path(info["path"])
        if path.is_dir():
            files = sorted([*path.glob("*.pth"), *path.glob("*.safetensors"), *path.glob("*.pt")])
            if not files:
                raise FileNotFoundError(f"no upscaler weights in {path}")
            path = files[0]
        desc = spandrel.ModelLoader().load_from_file(str(path))
        desc.model.eval()
        if cuda_available():
            desc.to("cuda")
            if desc.supports_half:
                desc.model.half()
        self._cache[key] = _Loaded(desc, int(info.get("vram_mb") or 1000), "upscaler", "gpu")
        return desc

    # ----------------------------------------------------------- video models
    def video_pipeline(self, info: dict[str, Any], memory_mode: str = "auto") -> ModelHandle:
        family = info.get("family")
        key = f"video:{info['id']}"
        cached = self._touch(key)
        if cached is not None:
            pipe, mode = cached
            return ModelHandle(key, info["id"], family, info, pipe, mode)
        if family in NATIVE_PIPELINES:
            return self._native_pipeline("video", info, memory_mode)
        if family not in ("wan", "wan22-ti2v"):
            raise ValueError(f"unsupported video family {family}")
        from diffusers import AutoencoderKLWan, WanPipeline
        from transformers import UMT5EncoderModel

        path = str(info["path"])
        est = int(info.get("vram_mb") or 9000)
        self._room_before_load(est, key)
        t0 = time.perf_counter()
        fast = fast_load()
        # Wan 2.2 A14B has two ~57 GB experts (high noise, low noise). Each is stored in FP8 as soon as
        # it has loaded and computed in bf16, which halves its memory and caps the load peak at one
        # bf16 expert.
        experts: dict[str, Any] = {}
        if (Path(path) / "transformer_2").is_dir():
            from diffusers import WanTransformer3DModel

            for sub in ("transformer", "transformer_2"):
                tr = WanTransformer3DModel.from_pretrained(path, subfolder=sub, dtype=torch.bfloat16, **fast)
                tr.enable_layerwise_casting(storage_dtype=torch.float8_e4m3fn, compute_dtype=torch.bfloat16)
                experts[sub] = tr
                self.collect()
        # Text encoder: bf16 on CPU; streamed to the GPU block-by-block when encoding.
        te = UMT5EncoderModel.from_pretrained(path, subfolder="text_encoder", dtype=torch.bfloat16, **fast)
        vae = AutoencoderKLWan.from_pretrained(path, subfolder="vae", dtype=torch.float32, **fast)
        pipe = WanPipeline.from_pretrained(path, vae=vae, text_encoder=te, dtype=torch.bfloat16, **fast, **experts)
        pipe.set_progress_bar_config(disable=True)
        pipe._sarness_default_scheduler = pipe.scheduler

        te_bytes_mb = sum(p.numel() * p.element_size() for p in te.parameters()) // (1024 * 1024)
        tr_bytes_mb = sum(p.numel() * p.element_size() for p in pipe.transformer.parameters()) // (1024 * 1024)
        self.make_room(est, keep={key})
        free = free_vram_mb()
        mode = memory_mode
        if mode == "auto" and unified_memory():
            mode = "gpu"
        if mode == "auto":
            if free >= te_bytes_mb + est + self.headroom_mb:
                mode = "gpu"
            elif free >= tr_bytes_mb + 4500 + self.headroom_mb:
                mode = "split"     # transformer resident, text encoder streamed
            else:
                mode = "offload"   # transformer blocks streamed as well
        if cuda_available():
            if mode == "gpu":
                pipe.to("cuda")
            else:
                pipe.vae.to("cuda")
                pipe.vae.enable_tiling()  # bounds decode VRAM at the cost of a little speed
                _stream_module(pipe.text_encoder, "leaf_level")
                if mode == "split":
                    pipe.transformer.to("cuda")
                elif mode == "offload":
                    _stream_module(pipe.transformer, "block_level")
                else:  # sequential
                    _stream_module(pipe.transformer, "leaf_level")
        log.info("loaded %s in %.1fs, mode=%s (text encoder %d MiB)", info["id"], time.perf_counter() - t0, mode, te_bytes_mb)
        self._cache[key] = _Loaded((pipe, mode), est, "video", mode)
        return ModelHandle(key, info["id"], family, info, pipe, mode)


    def _native_pipeline(self, kind: str, info: dict[str, Any], memory_mode: str) -> ModelHandle:
        """Loads a pipeline that runs end to end (Qwen-Image 2.1, LTX-2) in bf16."""
        import diffusers

        family = info["family"]
        key = f"{kind}:{info['id']}"
        cls = getattr(diffusers, NATIVE_PIPELINES[family], None)
        if cls is None:
            raise ValueError(f"this diffusers version ({diffusers.__version__}) has no {NATIVE_PIPELINES[family]}")
        est = int(info.get("vram_mb") or 30000)
        self._room_before_load(est, key)
        t0 = time.perf_counter()
        dtype = torch.bfloat16 if cuda_available() else torch.float32
        pipe = load_pipeline(cls, info["path"], dtype)
        pipe.set_progress_bar_config(disable=True)
        pipe._sarness_default_scheduler = pipe.scheduler
        self.make_room(est, keep={key})
        mode = self._choose_mode(memory_mode, est, keep={key})
        if mode == "split":
            mode = "offload"
        self._place(pipe, mode)
        log.info("loaded %s (%s) in %.1fs, mode=%s", info["id"], family, time.perf_counter() - t0, mode)
        self._cache[key] = _Loaded((pipe, mode), est, kind, mode)
        return ModelHandle(key, info["id"], family, info, pipe, mode)


def _stream_module(module: torch.nn.Module, offload_type: str = "leaf_level") -> None:
    """Keep ``module`` in CPU RAM and stream its weights to the GPU during forward.

    ``leaf_level`` works for any architecture (needed for T5, whose blocks are
    nested); ``block_level`` groups top-level ModuleLists (DiT transformer blocks).
    """
    try:
        from diffusers.hooks import apply_group_offloading

        kwargs = {"num_blocks_per_group": 1} if offload_type == "block_level" else {}
        apply_group_offloading(module, onload_device=torch.device("cuda"), offload_device=torch.device("cpu"),
                               offload_type=offload_type, use_stream=True, **kwargs)
        module._sarness_streamed = True
    except Exception as exc:  # pragma: no cover - depends on diffusers version
        log.warning("group offloading unavailable (%s); module will run on CPU", exc)
        module._sarness_streamed = False


# ---------------------------------------------------------------- LoRA & sched
def _adapter_name(model_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", model_id)[:48]


def apply_loras(handle: ModelHandle) -> None:
    pipe = handle.pipe
    loaded: dict[str, str] = pipe.__dict__.setdefault("_sarness_adapters", {})
    names, weights = [], []
    for ref in handle.loras:
        name = _adapter_name(ref.model_id)
        if name not in loaded:
            p = Path(ref.path)
            if p.is_dir():
                files = sorted(p.glob("*.safetensors"))
                if not files:
                    raise FileNotFoundError(f"no LoRA weights found in {p}")
                pipe.load_lora_weights(str(p), weight_name=files[0].name, adapter_name=name)
            else:
                pipe.load_lora_weights(str(p.parent), weight_name=p.name, adapter_name=name)
            loaded[name] = ref.path
        names.append(name)
        weights.append(ref.weight)
    if names:
        pipe.enable_lora()
        pipe.set_adapters(names, adapter_weights=weights)
    elif loaded:
        pipe.disable_lora()


def apply_scheduler(pipe: Any, spec: dict[str, Any] | None) -> None:
    import diffusers

    default = pipe.__dict__.get("_sarness_default_scheduler") or pipe.scheduler
    if not spec or spec.get("name") in (None, "default"):
        pipe.scheduler = default
        return
    cls_name, extra = SCHEDULERS[spec["name"]]
    cls = getattr(diffusers, cls_name)
    opts = {**extra, **(spec.get("options") or {})}
    pipe.scheduler = cls.from_config(default.config, **opts)
