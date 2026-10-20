"""Model management: catalog view, Hugging Face browsing, installs and removal."""

from __future__ import annotations

import fnmatch
import logging
import shutil
from pathlib import Path
from typing import Any

import httpx

from .. import hardware
from .catalog import CATALOG, HARDWARE_PRESETS, default_quant, get_entry
from .downloads import HF, DownloadError, DownloadManager, list_repo_files
from .registry import CATEGORIES, PIPELINE_FAMILIES, UNSUPPORTED_PIPELINES, ModelRegistry, repo_slug

log = logging.getLogger("sarness.models")

DIFFUSERS_SKIP = ("*.ckpt", "*.bin", "*.onnx", "*.onnx_data", "*.msgpack", "*.h5", "*.ot", "*.pb", "flax_model*",
                  "*.md", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.webp", "assets/*", "examples/*", "images/*")


def compatibility(need_mb: int | None, offload_mb: int | None, total_mb: int | None) -> dict[str, Any]:
    if not need_mb or not total_mb:
        return {"level": "unknown", "label": "Unknown requirements"}
    usable = total_mb - 700
    if need_mb <= usable:
        return {"level": "fits", "label": "Runs fully on GPU"}
    if offload_mb and offload_mb <= usable:
        return {"level": "offload", "label": "Runs with CPU offload (slower)"}
    return {"level": "too_large", "label": "Likely too large for this GPU — expect very slow sequential offload or failure"}


class ModelService:
    def __init__(self, registry: ModelRegistry, downloads: DownloadManager, models_dir: Path, token_getter):
        self.registry = registry
        self.downloads = downloads
        self.models_dir = models_dir
        self.token_getter = token_getter

    def gpu_total(self) -> int | None:
        return hardware.vram_total_mb()

    # ------------------------------------------------------------ catalog
    def catalog(self) -> list[dict[str, Any]]:
        installed = {m["id"]: m for m in self.registry.list()}
        active = {d["model_id"]: d for d in self.downloads.list() if d["status"] in ("queued", "downloading", "paused", "failed")}
        total = self.gpu_total()
        out = []
        for e in CATALOG:
            item = {k: v for k, v in e.items() if k != "files"}
            if e.get("quantizations"):
                qs = []
                for q in e["quantizations"]:
                    mid = f"{e['id']}@{q['id']}"
                    qs.append({**q, "model_id": mid, "installed": mid in installed, "download": active.get(mid),
                               "compatibility": compatibility(q["vram_mb"], None, total)})
                item["quantizations"] = qs
                item["installed"] = any(q["installed"] for q in qs)
                dq = default_quant(e)
                item["compatibility"] = compatibility(dq["vram_mb"] if dq else None, None, total)
            else:
                item["model_id"] = e["id"]
                item["installed"] = e["id"] in installed
                item["download"] = active.get(e["id"])
                item["compatibility"] = compatibility(e.get("vram_mb"), e.get("vram_offload_mb"), total)
            out.append(item)
        return out

    def presets(self) -> list[dict[str, Any]]:
        total = self.gpu_total() or 0
        best = None
        for p in HARDWARE_PRESETS:
            if total >= p["vram_mb"] - 600:
                best = p["id"]
        return [{**p, "matches_this_server": p["id"] == best} for p in HARDWARE_PRESETS]

    def install_catalog(self, catalog_id: str, quant: str | None = None) -> dict[str, Any]:
        e = get_entry(catalog_id)
        if not e:
            raise DownloadError(f"unknown catalog entry {catalog_id}")
        dest = self.models_dir / e["category"] / repo_slug(e["repo"])
        if e.get("quantizations"):
            q = next((q for q in e["quantizations"] if q["id"] == quant), None) if quant else default_quant(e)
            if not q:
                raise DownloadError(f"unknown quantization {quant}")
            return self.downloads.create(model_id=f"{e['id']}@{q['id']}", name=f"{e['name']} · {q['id']}",
                                         category=e["category"], repo=e["repo"], patterns=q["files"], dest=dest,
                                         catalog_id=e["id"])
        return self.downloads.create(model_id=e["id"], name=e["name"], category=e["category"], repo=e["repo"],
                                     patterns=e.get("files"), dest=dest, catalog_id=e["id"], sources=e.get("sources"))

    # ----------------------------------------------------------- HF browse
    def hf_search(self, q: str, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"search": q, "sort": "downloads", "limit": limit, "full": "false"}
        if kind == "llm":
            params["filter"] = "gguf"
        elif kind in ("image", "video"):
            params["filter"] = "diffusers"
        elif kind == "lora":
            params["filter"] = "lora"
        headers = {"Authorization": f"Bearer {self.token_getter()}"} if self.token_getter() else {}
        r = httpx.get(f"{HF}/api/models", params=params, headers=headers, timeout=20)
        r.raise_for_status()
        out = []
        for m in r.json():
            out.append({"id": m["id"], "downloads": m.get("downloads"), "likes": m.get("likes"),
                        "pipeline_tag": m.get("pipeline_tag"), "library": m.get("library_name"),
                        "tags": [t for t in (m.get("tags") or []) if ":" not in t][:12],
                        "updated": m.get("lastModified"), "gated": m.get("gated", False)})
        return out

    def hf_inspect(self, repo: str) -> dict[str, Any]:
        files = list_repo_files(repo, self.token_getter() or None)
        paths = {f["path"] for f in files}
        total = self.gpu_total()
        result: dict[str, Any] = {"repo": repo, "files": files, "total_bytes": sum(f["size"] for f in files)}
        ggufs = [f for f in files if f["path"].endswith(".gguf") and not f["path"].split("/")[-1].startswith(("mmproj", "mtp-"))]
        mmproj = [f for f in files if f["path"].split("/")[-1].startswith("mmproj") and f["path"].endswith(".gguf")]
        if ggufs:
            result["format"] = "gguf"
            result["category"] = "llm"
            options = []
            for f in sorted(ggufs, key=lambda f: f["size"]):
                if "-0000" in f["path"] and "-00001-of-" not in f["path"]:
                    continue
                shards = [f]
                if "-00001-of-" in f["path"]:
                    prefix = f["path"].split("-00001-of-")[0]
                    shards = [g for g in ggufs if g["path"].startswith(prefix + "-0000")]
                size = sum(s["size"] for s in shards)
                need = int(size / 2**20 * 1.08) + 900
                mm = sorted(mmproj, key=lambda m: m["size"])[:1]
                options.append({"label": f["path"].split("/")[-1].replace(".gguf", ""),
                                "files": [s["path"] for s in shards] + [m["path"] for m in mm],
                                "size_bytes": size + sum(m["size"] for m in mm), "vram_mb": need,
                                "vision": bool(mm), "compatibility": compatibility(need, None, total)})
            result["options"] = options
            return result
        if "model_index.json" in paths:
            try:
                r = httpx.get(f"{HF}/{repo}/resolve/main/model_index.json", timeout=15, follow_redirects=True,
                              headers={"Authorization": f"Bearer {self.token_getter()}"} if self.token_getter() else {})
                cls = r.json().get("_class_name", "")
            except Exception:
                cls = ""
            fam = PIPELINE_FAMILIES.get(cls)
            result["format"] = "diffusers"
            result["pipeline"] = cls
            result["supported"] = fam is not None
            if not fam:
                result["unsupported_reason"] = (f"{cls} is recognised but its runtime adapter is not implemented yet"
                                                if cls in UNSUPPORTED_PIPELINES else f"{cls or 'unknown'} pipelines are not supported")
            result["category"] = fam[0] if fam else ("video" if "video" in cls.lower() else "image")
            has_fp16 = any(".fp16." in p for p in paths)
            chosen = [f for f in files if not any(fnmatch.fnmatch(f["path"], pat) for pat in DIFFUSERS_SKIP)
                      and "/" in f["path"] or f["path"] == "model_index.json"]
            if has_fp16:
                # Keep fp16 weights where a component has them; drop the fp32 duplicates.
                comps_fp16 = {f["path"].split("/")[0] for f in chosen if ".fp16." in f["path"]}
                chosen = [f for f in chosen if ".fp16." in f["path"] or f["path"].split("/")[0] not in comps_fp16
                          or not f["path"].endswith((".safetensors", ".bin"))]
            size = sum(f["size"] for f in chosen)
            need = int(size / 2**20 * 1.15) + 1200
            result["options"] = [{"label": "fp16 weights" if has_fp16 else "default weights",
                                  "files": [f["path"] for f in chosen], "size_bytes": size, "vram_mb": need,
                                  "compatibility": compatibility(need, int(need * 0.55), total)}]
            return result
        singles = [f for f in files if f["path"].endswith((".safetensors", ".pth")) and "/" not in f["path"]]
        result["format"] = "files"
        result["category"] = None
        result["options"] = [{"label": f["path"], "files": [f["path"]], "size_bytes": f["size"],
                              "vram_mb": int(f["size"] / 2**20 * 1.2) + 500,
                              "compatibility": compatibility(int(f["size"] / 2**20 * 1.2) + 500, None, total)}
                             for f in sorted(singles, key=lambda f: f["path"])]
        return result

    def install_hf(self, repo: str, files: list[str], category: str, name: str | None = None) -> dict[str, Any]:
        if category not in CATEGORIES:
            raise DownloadError(f"invalid category {category}")
        if not files:
            raise DownloadError("select at least one file")
        for f in files:
            if f.startswith("/") or ".." in Path(f).parts:
                raise DownloadError("invalid file path")
        dest = self.models_dir / category / repo_slug(repo)
        label = name or repo.split("/")[-1]
        if category == "llm":
            main = next((f for f in files if not f.split("/")[-1].startswith("mmproj")), files[0])
            model_id = "gguf:" + Path(main).stem.lower()
        else:
            from .registry import slugify

            model_id = f"{category}:{slugify(repo_slug(repo))}"
        return self.downloads.create(model_id=model_id, name=label, category=category, repo=repo, patterns=files,
                                     dest=dest, meta={"source": "huggingface"})

    # -------------------------------------------------------------- remove
    def delete(self, model_id: str) -> None:
        info = self.registry.get(model_id)
        if not info:
            raise FileNotFoundError(model_id)
        if info.get("remote"):
            raise PermissionError("this model belongs to the server's AI gateway and is managed there, not by Sarness")
        path = Path(info["path"]).resolve()
        root = self.models_dir.resolve()
        if root not in path.parents:
            raise PermissionError("models outside the managed models directory are never deleted by Sarness")
        if info["category"] == "llm" and path.is_file():
            path.unlink()
            remaining = [p for p in path.parent.glob("*.gguf") if not p.name.startswith(("mmproj", "mtp-"))]
            if not remaining:
                shutil.rmtree(path.parent, ignore_errors=True)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        self.registry.scan()

    def disk_usage(self) -> dict[str, Any]:
        by_cat: dict[str, int] = {}
        for m in self.registry.list():
            by_cat[m["category"]] = by_cat.get(m["category"], 0) + int(m.get("size") or 0)
        return {"by_category": by_cat, "total_bytes": sum(by_cat.values()),
                "free_bytes": hardware.disk_free_bytes(self.models_dir), "models_dir": str(self.models_dir)}
