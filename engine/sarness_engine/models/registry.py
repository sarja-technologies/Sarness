"""Installed-model registry: discovery, identification and lookup.

Model directory layout (under ``<data>/models``)::

    llm/<repo-slug>/*.gguf            (mmproj-*.gguf are paired vision projectors)
    image/<repo-slug>/model_index.json  or  image/*.safetensors (single file)
    video/<repo-slug>/model_index.json
    lora/*.safetensors | lora/<repo-slug>/*.safetensors
    upscaler/*.pth|*.safetensors | upscaler/<repo-slug>/*.pth
    vae/<repo-slug>/config.json | vae/*.safetensors
    controlnet/<repo-slug>/config.json
"""

from __future__ import annotations

import json
import logging
import re
import struct
import threading
from pathlib import Path
from typing import Any

from ..db import Database, now
from .catalog import CATALOG, NATIVE_IMAGE_CAPS, get_entry
from .downloads import MARKER_PREFIX, pending_files

log = logging.getLogger("sarness.registry")

CATEGORIES = ("llm", "image", "video", "upscaler", "vae", "lora", "controlnet")

PIPELINE_FAMILIES = {
    "StableDiffusionXLPipeline": ("image", "sdxl"),
    "StableDiffusionPipeline": ("image", "sd15"),
    "WanPipeline": ("video", "wan"),
    "WanImageToVideoPipeline": ("video", "wan"),
    "QwenImage21Pipeline": ("image", "qwenimage21"),
    "LTX2Pipeline": ("video", "ltx2"),
    "LTX2ImageToVideoPipeline": ("video", "ltx2"),
    "MiniMaxH3ModularPipeline": ("video", "minimaxh3"),
    "QwenImagePipeline": ("image", "qwenimage"),
    "QwenImageEditPlusPipeline": ("image", "qwenimage-edit"),
    "ZImagePipeline": ("image", "zimage"),
    "Flux2KleinPipeline": ("image", "flux2klein"),
    "Flux2Pipeline": ("image", "flux2"),
    "HunyuanVideo15Pipeline": ("video", "hunyuanvideo15"),
    "HunyuanVideo15ImageToVideoPipeline": ("video", "hunyuanvideo15-i2v"),
}
UNSUPPORTED_PIPELINES = {
    "FluxPipeline", "StableDiffusion3Pipeline", "LTXPipeline", "HunyuanVideoPipeline", "CogVideoXPipeline",
    "PixArtSigmaPipeline", "SanaPipeline",
}


def is_projector(name: str) -> bool:
    """Vision projector GGUFs: "mmproj-F16.gguf", or "<model>.mmproj-f16.gguf" (mradermacher's naming)."""
    return name.startswith("mmproj") or ".mmproj" in name


def is_companion(name: str) -> bool:
    """GGUFs that accompany a model rather than being one: projectors and MTP heads."""
    return is_projector(name) or name.startswith("mtp-")


def repo_slug(repo: str) -> str:
    return repo.replace("/", "--")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-")[:80]


def _dir_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and ".cache" not in f.parts)


def safetensors_keys(path: Path, limit: int = 4000) -> list[str]:
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n > 100_000_000:
                return []
            header = json.loads(f.read(n))
        return [k for k in header if k != "__metadata__"][:limit]
    except Exception:
        return []


def guess_checkpoint_family(path: Path) -> str | None:
    keys = safetensors_keys(path)
    if any(k.startswith("conditioner.embedders.1") for k in keys):
        return "sdxl"
    if any(k.startswith("cond_stage_model.") for k in keys) and any(k.startswith("model.diffusion_model.") for k in keys):
        return "sd15"
    return None


def gguf_metadata(path: Path) -> dict[str, Any]:
    """Reads a few GGUF header fields (architecture, context length) without llama.cpp."""
    out: dict[str, Any] = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return out
            version = struct.unpack("<I", f.read(4))[0]
            if version < 2:
                return out
            _tensors, kv = struct.unpack("<QQ", f.read(16))

            def rstr():
                (ln,) = struct.unpack("<Q", f.read(8))
                return f.read(ln).decode("utf-8", "replace")

            scalar = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}

            def rval(t):
                if t in scalar:
                    fmt = scalar[t]
                    return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
                if t == 8:
                    return rstr()
                if t == 9:
                    at, = struct.unpack("<I", f.read(4))
                    n, = struct.unpack("<Q", f.read(8))
                    if at in scalar:
                        f.seek(struct.calcsize(scalar[at]) * n, 1)
                    else:
                        for _ in range(n):
                            rval(at)
                    return None
                raise ValueError(t)

            for _ in range(min(kv, 4096)):
                key = rstr()
                (t,) = struct.unpack("<I", f.read(4))
                val = rval(t)
                if key == "general.architecture":
                    out["architecture"] = val
                elif key == "general.name":
                    out["name"] = val
                elif key.endswith(".context_length"):
                    out["context_length"] = val
                elif key == "general.file_type":
                    out["file_type"] = val
                elif key.endswith(".block_count"):
                    out["layers"] = val
                if len(out) >= 5:
                    break
    except Exception as exc:
        log.debug("gguf header read failed for %s: %s", path, exc)
    return out


class ModelRegistry:
    def __init__(self, db: Database, models_dir: Path, extra_dirs: list[str] | None = None):
        self.db = db
        self.models_dir = models_dir
        self.extra_dirs = [Path(d).expanduser() for d in (extra_dirs or [])]
        # Optional callable returning remote (gateway) model rows; see remote/gateway.py.
        self.remote_provider = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------- discovery
    def scan(self) -> list[dict[str, Any]]:
        with self._lock:
            found: dict[str, dict[str, Any]] = {}
            roots = [self.models_dir, *self.extra_dirs]
            for root in roots:
                for cat in CATEGORIES:
                    d = root / cat
                    if d.is_dir():
                        for m in self._scan_category(cat, d):
                            found.setdefault(m["id"], m)
            if self.remote_provider:
                for m in self.remote_provider() or []:
                    found.setdefault(m["id"], m)
            existing = {r["id"]: r for r in self.db.all("SELECT * FROM models")}
            for mid, m in found.items():
                row = {
                    "id": mid, "catalog_id": m.get("catalog_id"), "name": m["name"], "category": m["category"],
                    "family": m.get("family"), "format": m["format"], "path": str(m["path"]),
                    "size": m.get("size"), "source": m.get("source") or {}, "meta": m.get("meta") or {},
                }
                if mid in existing:
                    self.db.update("models", mid, row)
                else:
                    self.db.insert("models", {**row, "installed_at": now()})
            for mid in existing:
                if mid not in found:
                    self.db.execute("DELETE FROM models WHERE id = ?", [mid])
            return self.list()

    def _scan_category(self, cat: str, d: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if cat == "llm":
            pending = {p for m in d.rglob(MARKER_PREFIX + "*") for p in pending_files(m.parent)}
            for f in sorted(d.rglob("*.gguf")):
                if is_companion(f.name) or ".partial" in f.name:
                    continue
                split = f.name.split("-00001-of-")[0] + "-000" if "-00001-of-" in f.name else None
                if f in pending or split and any(p.parent == f.parent and p.name.startswith(split) for p in pending):
                    continue  # still downloading (a split GGUF counts until every shard is in)
                if re.search(r"-0000[2-9]-of-", f.name):
                    continue  # later shards of split GGUFs
                mmproj = _companion(f, d, "mmproj*.gguf") or _companion(f, d, "*.mmproj*.gguf")
                entry, quant = self._match_gguf(f)
                meta = gguf_metadata(f)
                meta["mmproj"] = str(mmproj[0]) if mmproj else None
                # Multi-token-prediction head for speculative decoding (llama.cpp --spec-type draft-mtp).
                mtp = _companion(f, d, "mtp-*.gguf") or sorted((f.parent / "MTP").glob("mtp-*.gguf"))
                meta["mtp"] = str(mtp[0]) if mtp else None
                mid = f"{entry['id']}@{quant}" if entry else "gguf:" + slugify(f.stem)
                out.append({
                    "id": mid, "catalog_id": entry["id"] if entry else None,
                    "name": (f"{entry['name']} · {quant}" if entry else (meta.get("name") or f.stem)),
                    "category": "llm", "family": "gguf", "format": "gguf", "path": f,
                    "size": f.stat().st_size + (mmproj[0].stat().st_size if mmproj else 0), "meta": meta,
                })
            return out
        for p in sorted(d.iterdir()):
            if p.name.startswith(".") or p.name.endswith(".partial"):
                continue
            if p.is_dir() and any(p.glob(MARKER_PREFIX + "*")):
                continue  # still downloading
            item = self._identify(cat, p)
            if item:
                out.append(item)
        return out

    def _match_gguf(self, f: Path) -> tuple[dict[str, Any] | None, str | None]:
        # Fine-tunes often keep the base model's file names (an abliterated Flash Next ships
        # "Qwen3.8-Flash-Next-UD-IQ4_XS-…"), so a file inside a repo folder only matches that repo's entry.
        repo_dirs = {p.name for p in f.parents if "--" in p.name}
        for e in CATALOG:
            if repo_dirs and repo_slug(e.get("repo") or "") not in repo_dirs:
                continue
            for q in e.get("quantizations") or []:
                if f.name == Path(q["files"][0]).name:
                    return e, q["id"]
        return None, None

    def _match_dir(self, p: Path) -> dict[str, Any] | None:
        for e in CATALOG:
            if e.get("repo") and repo_slug(e["repo"]) == p.name:
                return e
        return None

    def _identify(self, cat: str, p: Path) -> dict[str, Any] | None:
        entry = self._match_dir(p) if p.is_dir() else None
        base = {"category": cat, "path": p, "size": _dir_size(p)}
        if entry:
            return {**base, "id": entry["id"], "catalog_id": entry["id"], "name": entry["name"],
                    "family": entry["family"], "format": entry["format"]}
        mid = f"{cat}:{slugify(p.stem if p.is_file() else p.name)}"
        if cat in ("image", "video"):
            if p.is_dir() and (p / "model_index.json").exists():
                try:
                    cls = json.loads((p / "model_index.json").read_text()).get("_class_name", "")
                except Exception:
                    cls = ""
                fam = PIPELINE_FAMILIES.get(cls)
                if not fam:
                    return {**base, "id": mid, "name": p.name, "family": None, "format": "diffusers",
                            "meta": {"unsupported": f"{cls or 'unknown'} pipelines are not supported yet"}}
                fam_cat, family = fam
                if family == "wan" and self._is_wan22_ti2v(p):
                    family = "wan22-ti2v"
                return {**base, "id": mid, "name": p.name.split("--")[-1], "family": family, "format": "diffusers",
                        "category": fam_cat}
            if p.is_file() and p.suffix in (".safetensors", ".ckpt"):
                family = guess_checkpoint_family(p) if p.suffix == ".safetensors" else None
                meta = {} if family else {"unsupported": "could not identify the checkpoint architecture"}
                return {**base, "id": mid, "name": p.stem, "family": family, "format": "safetensors", "meta": meta}
            return None
        if cat == "lora":
            if p.is_file() and p.suffix == ".safetensors" or p.is_dir() and any(p.glob("*.safetensors")):
                return {**base, "id": mid, "name": p.stem if p.is_file() else p.name.split("--")[-1],
                        "family": "lora", "format": "safetensors"}
        if cat == "upscaler":
            if p.is_file() and p.suffix in (".pth", ".safetensors", ".pt") or p.is_dir():
                return {**base, "id": mid, "name": p.stem if p.is_file() else p.name.split("--")[-1],
                        "family": "esrgan", "format": p.suffix.lstrip(".") or "dir"}
        if cat in ("vae", "controlnet"):
            if p.is_dir() and (p / "config.json").exists() or p.is_file() and p.suffix == ".safetensors":
                return {**base, "id": mid, "name": p.stem if p.is_file() else p.name.split("--")[-1],
                        "family": f"{cat}-sdxl", "format": "diffusers" if p.is_dir() else "safetensors"}
        return None

    @staticmethod
    def _is_wan22_ti2v(p: Path) -> bool:
        try:
            cfg = json.loads((p / "transformer" / "config.json").read_text())
            return cfg.get("in_channels") == 48
        except Exception:
            return False

    # -------------------------------------------------------------- lookup
    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM models ORDER BY category, name")
        return [self._enrich(r) for r in rows]

    def get(self, model_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM models WHERE id = ?", [model_id])
        return self._enrich(row) if row else None

    def _enrich(self, row: dict[str, Any]) -> dict[str, Any]:
        entry = get_entry(row["catalog_id"]) if row.get("catalog_id") else None
        info = dict(row)
        meta = row.get("meta") or {}
        info["supported"] = not meta.get("unsupported") and row.get("family") is not None
        info["unsupported_reason"] = meta.get("unsupported")
        if entry:
            for k in ("description", "capabilities", "defaults", "limits", "vram_mb", "vram_offload_mb", "license",
                      "tags", "notes", "base", "llama_args"):
                if k in entry:
                    info[k] = entry[k]
            if row["category"] == "llm":
                quant = row["id"].split("@", 1)[-1]
                q = next((q for q in entry.get("quantizations", []) if q["id"] == quant), None)
                if q:
                    info["vram_mb"] = q["vram_mb"]
                    info["quantization"] = quant
        elif meta.get("remote"):
            info.update(meta.get("profile") or {})
            info["remote"] = True
        else:
            info.setdefault("capabilities", self._default_caps(row))
            info.setdefault("defaults", _FAMILY_DEFAULTS.get(row.get("family") or "", {}))
            info.setdefault("limits", _FAMILY_LIMITS.get(row.get("family") or "", {}))
            size_mb = (row.get("size") or 0) // (1024 * 1024)
            info.setdefault("vram_mb", int(size_mb * 1.25) + 800 if size_mb else None)
            info.setdefault("vram_offload_mb", int(size_mb * 0.6) + 800 if size_mb else None)
        if row["category"] == "llm":
            caps = dict(info.get("capabilities") or {})
            caps["vision"] = bool(meta.get("mmproj"))
            caps.setdefault("context_max", meta.get("context_length") or 8192)
            caps.setdefault("default_ctx", min(8192, caps["context_max"]))
            info["capabilities"] = caps
        return info

    @staticmethod
    def _default_caps(row: dict[str, Any]) -> dict[str, Any]:
        fam = row.get("family")
        if fam in ("sdxl", "sd15"):
            return {"modes": ["txt2img", "img2img", "inpaint"], "lora": True, "controlnet": True, "negative_prompt": True}
        if fam == "wan":
            return {"modes": ["t2v"], "negative_prompt": True, "start_frame": False, "end_frame": False, "motion": []}
        if fam == "wan22-ti2v":
            return {"modes": ["t2v", "i2v"], "negative_prompt": True, "start_frame": True, "end_frame": False, "motion": []}
        if fam == "qwenimage21":
            return {"modes": ["txt2img"], "lora": False, "controlnet": False, "negative_prompt": True}
        if fam in NATIVE_IMAGE_CAPS:
            return dict(NATIVE_IMAGE_CAPS[fam])
        if fam == "hunyuanvideo15":
            return {"modes": ["t2v"], "negative_prompt": True, "start_frame": False, "end_frame": False, "motion": []}
        if fam == "hunyuanvideo15-i2v":
            return {"modes": ["i2v"], "negative_prompt": True, "start_frame": True, "end_frame": False, "motion": []}
        if fam == "ltx2":
            return {"modes": ["t2v", "i2v"], "negative_prompt": True, "start_frame": True, "end_frame": False,
                    "motion": [], "audio": True}
        if fam == "minimaxh3":
            return {"modes": ["t2v", "i2v"], "negative_prompt": False, "start_frame": True, "end_frame": True,
                    "motion": [], "audio": True}
        return {}

    def worker_info(self, model_id: str) -> dict[str, Any] | None:
        """Minimal JSON-safe description handed to the GPU worker."""
        info = self.get(model_id)
        if not info:
            return None
        keys = ("id", "name", "category", "family", "format", "path", "vram_mb", "vram_offload_mb",
                "capabilities", "limits", "defaults", "catalog_id")
        return {k: info.get(k) for k in keys}


_FAMILY_DEFAULTS = {
    "sdxl": {"steps": 30, "guidance": 6.0, "width": 1024, "height": 1024, "scheduler": "dpmpp_2m_karras"},
    "sd15": {"steps": 30, "guidance": 7.0, "width": 512, "height": 512, "scheduler": "dpmpp_2m_karras"},
    "wan": {"steps": 30, "guidance": 5.0, "width": 832, "height": 480, "frames": 33, "fps": 16, "flow_shift": 3.0},
    "wan22-ti2v": {"steps": 40, "guidance": 5.0, "width": 1280, "height": 704, "frames": 49, "fps": 24, "flow_shift": 5.0},
    "qwenimage21": {"steps": 40, "guidance": 4.0, "width": 1024, "height": 1024},
    "ltx2": {"steps": 30, "guidance": 3.0, "width": 768, "height": 512, "frames": 121, "fps": 24},
    "minimaxh3": {"steps": 30, "guidance": 1.0, "width": 1344, "height": 768, "frames": 124, "fps": 24},
    "qwenimage": {"steps": 50, "guidance": 4.0, "width": 1328, "height": 1328},
    "qwenimage-edit": {"steps": 40, "guidance": 4.0, "width": 1024, "height": 1024},
    "zimage": {"steps": 30, "guidance": 4.0, "width": 1024, "height": 1024},
    "flux2klein": {"steps": 4, "guidance": 1.0, "width": 1024, "height": 1024},
    "flux2": {"steps": 28, "guidance": 4.0, "width": 1024, "height": 1024},
    "hunyuanvideo15": {"steps": 50, "guidance": 6.0, "width": 1280, "height": 720, "frames": 121, "fps": 24},
    "hunyuanvideo15-i2v": {"steps": 50, "guidance": 6.0, "width": 1280, "height": 720, "frames": 121, "fps": 24},
}
_FAMILY_LIMITS = {
    "sdxl": {"steps": [1, 100], "guidance": [0, 20], "min_side": 512, "max_side": 1536, "multiple": 8},
    "sd15": {"steps": [1, 100], "guidance": [0, 20], "min_side": 256, "max_side": 1024, "multiple": 8},
    "wan": {"steps": [4, 60], "frames": [5, 81], "frame_step": 4, "multiple": 16},
    "wan22-ti2v": {"steps": [4, 60], "frames": [5, 121], "frame_step": 4, "multiple": 32},
    "qwenimage21": {"steps": [4, 80], "guidance": [1, 10], "min_side": 512, "max_side": 2048, "multiple": 16},
    "ltx2": {"steps": [4, 60], "frames": [9, 257], "frame_step": 8, "multiple": 32},
    "minimaxh3": {"steps": [8, 60], "frames": [124, 345], "frame_step": 1, "multiple": 32},
    "qwenimage": {"steps": [4, 80], "guidance": [1, 10], "min_side": 512, "max_side": 2048, "multiple": 16},
    "qwenimage-edit": {"steps": [4, 80], "guidance": [1, 10], "min_side": 512, "max_side": 2048, "multiple": 16},
    "zimage": {"steps": [4, 80], "guidance": [0, 10], "min_side": 512, "max_side": 2048, "multiple": 16},
    "flux2klein": {"steps": [1, 50], "guidance": [0, 10], "min_side": 256, "max_side": 2048, "multiple": 16},
    "flux2": {"steps": [4, 80], "guidance": [1, 10], "min_side": 256, "max_side": 2048, "multiple": 16},
    "hunyuanvideo15": {"steps": [4, 60], "frames": [5, 241], "frame_step": 4, "multiple": 16},
    "hunyuanvideo15-i2v": {"steps": [4, 60], "frames": [5, 241], "frame_step": 4, "multiple": 16},
}


def _companion(model: Path, category_dir: Path, pattern: str) -> list[Path]:
    """Files matching ``pattern`` next to ``model`` or in a parent folder up to the repo folder
    (quantisations often live in sub-folders while the projector / MTP head sits at the root)."""
    d = model.parent
    found = sorted(d.glob(pattern))
    # Walk up while still strictly inside the category folder, so another model's files never match.
    while not found and d != category_dir and category_dir in d.parent.parents:
        d = d.parent
        found = sorted(d.glob(pattern))
    return found
