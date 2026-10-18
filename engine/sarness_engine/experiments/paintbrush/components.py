"""Pretrained weights God's Paintbrush uses: what, where from, under which licence,
whether they are installed, and a background installer (API process; no torch).

Weights live under ``<models_dir>/experiments/gods-paintbrush/<component>/``,
outside the categories the model registry scans, so they never show up as
ordinary image models.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger("sarness.experiments.paintbrush")

GDRIVE_URL = "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    stage: str                       # live | refine
    description: str
    license: str
    source: str
    url: str
    size_bytes: int
    required_files: tuple[str, ...]
    gdrive: dict[str, str] = field(default_factory=dict)       # file name → Google Drive file id
    sha256: dict[str, str] = field(default_factory=dict)       # file name → expected digest
    hf_repo: str = ""
    hf_revision: str = ""
    hf_allow: tuple[str, ...] = ()


COMPONENTS: tuple[Component, ...] = (
    Component(
        id="spade-landscapes-512", name="GauGAN (SPADE) landscapes, 512 px", stage="live",
        description=("SPADE generator trained for 50 epochs on ~41k Flickr landscape photos with NVlabs' official "
                     "SPADE code (COCO-Stuff labels, 512 px). Community checkpoint shared in NVlabs/SPADE issue #17 "
                     "after NVIDIA did not release its own landscape weights; used by mcheng89/gaugan."),
        license="CC BY-NC-SA 4.0 (non-commercial, inherited from NVlabs SPADE)",
        source="NVlabs/SPADE issue #17 (Gene Kogan and contributors, 2019)",
        url="https://github.com/NVlabs/SPADE/issues/17",
        size_bytes=390_184_744 + 4_083,
        required_files=("latest_net_G.pth", "opt.txt"),
        gdrive={"latest_net_G.pth": "15VSa2m2F6Ch0NpewDR7mkKAcXlMgDi5F", "opt.txt": "1zm26Oct3KaqO0dTW2dC_lNg8awsyo9sP"},
        sha256={"latest_net_G.pth": "9dc754be4e844d40136399d5fc2cef12fe22ff4f0620a75fa1a58d49dd87167a"},
    ),
    Component(
        id="sd15", name="Stable Diffusion 1.5 (fp16 weights)", stage="refine",
        description="Base text-to-image model the segmentation ControlNet was trained on.",
        license="CreativeML OpenRAIL-M",
        source="stable-diffusion-v1-5/stable-diffusion-v1-5 (official mirror of runwayml/stable-diffusion-v1-5)",
        url="https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5",
        size_bytes=2_135_160_000,
        required_files=("model_index.json", "scheduler/scheduler_config.json", "tokenizer/vocab.json",
                        "tokenizer/merges.txt", "text_encoder/model.fp16.safetensors",
                        "unet/diffusion_pytorch_model.fp16.safetensors", "vae/diffusion_pytorch_model.fp16.safetensors"),
        sha256={"unet/diffusion_pytorch_model.fp16.safetensors":
                "c83908253f9a64d08c25fc90874c9c8aef9a329ce1ca5fb909d73b0c83d1ea21"},
        hf_repo="stable-diffusion-v1-5/stable-diffusion-v1-5", hf_revision="451f4fe16113bff5a5d2269ed5ad43b0592e9a14",
        hf_allow=("model_index.json", "scheduler/*", "tokenizer/*", "feature_extractor/*", "text_encoder/config.json",
                  "text_encoder/model.fp16.safetensors", "unet/config.json",
                  "unet/diffusion_pytorch_model.fp16.safetensors", "vae/config.json",
                  "vae/diffusion_pytorch_model.fp16.safetensors"),
    ),
    Component(
        id="controlnet-seg", name="ControlNet 1.1 Segmentation (fp16)", stage="refine",
        description="lllyasviel's ControlNet v1.1 conditioned on ADE20K/COCO segmentation maps.",
        license="OpenRAIL (CreativeML OpenRAIL-M text)",
        source="lllyasviel/control_v11p_sd15_seg", url="https://huggingface.co/lllyasviel/control_v11p_sd15_seg",
        size_bytes=722_599_636,
        required_files=("config.json", "diffusion_pytorch_model.fp16.safetensors"),
        sha256={"diffusion_pytorch_model.fp16.safetensors":
                "23809d298edb48bf97ce95c6b8c4a69a749e57bf3838d866fbdc45f8fa11f6f4"},
        hf_repo="lllyasviel/control_v11p_sd15_seg", hf_revision="210682041b41097834006a910b8773f423343900",
        hf_allow=("config.json", "diffusion_pytorch_model.fp16.safetensors"),
    ),
)

BY_ID = {c.id: c for c in COMPONENTS}


def root_dir(models_dir: Path) -> Path:
    return Path(models_dir) / "experiments" / "gods-paintbrush"


def component_dir(models_dir: Path, cid: str) -> Path:
    return root_dir(models_dir) / cid


def installed(models_dir: Path, c: Component) -> bool:
    d = component_dir(models_dir, c.id)
    return all((d / f).is_file() and (d / f).stat().st_size > 0 for f in c.required_files)


def describe(models_dir: Path) -> list[dict[str, Any]]:
    out = []
    for c in COMPONENTS:
        out.append({"id": c.id, "name": c.name, "stage": c.stage, "description": c.description,
                    "license": c.license, "source": c.source, "url": c.url, "size_bytes": c.size_bytes,
                    "installed": installed(models_dir, c)})
    return out


def worker_paths(models_dir: Path) -> dict[str, str]:
    return {c.id: str(component_dir(models_dir, c.id)) for c in COMPONENTS}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


class Installer:
    """Downloads missing components one at a time in a background thread."""

    def __init__(self, models_dir: Path, hf_token: Callable[[], str], publish: Callable[[dict[str, Any]], None]):
        self.models_dir = Path(models_dir)
        self.hf_token = hf_token
        self.publish = publish
        self.state: dict[str, Any] = {"running": False, "component": None, "done_bytes": 0, "total_bytes": 0,
                                      "error": None, "finished_at": None}
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def start(self, only: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.state["running"]:
                return dict(self.state)
            todo = [c for c in COMPONENTS if (not only or c.id in only) and not installed(self.models_dir, c)]
            if not todo:
                return dict(self.state)
            free = shutil.disk_usage(self.models_dir if self.models_dir.exists() else self.models_dir.parent).free
            need = sum(c.size_bytes for c in todo)
            if free < need + 2 * 2**30:
                raise RuntimeError(f"not enough disk space: {need / 1e9:.1f} GB needed plus 2 GB headroom, "
                                   f"{free / 1e9:.1f} GB free")
            self._cancel.clear()
            self.state.update(running=True, component=None, done_bytes=0, total_bytes=need, error=None,
                              finished_at=None)
        threading.Thread(target=self._run, args=(todo,), daemon=True, name="paintbrush-install").start()
        return dict(self.state)

    def cancel(self) -> None:
        self._cancel.set()

    def _emit(self) -> None:
        self.publish(dict(self.state))

    def _run(self, todo: list[Component]) -> None:
        base = 0
        try:
            for c in todo:
                if self._cancel.is_set():
                    raise RuntimeError("cancelled")
                self.state["component"] = c.id
                self._emit()
                d = component_dir(self.models_dir, c.id)
                d.mkdir(parents=True, exist_ok=True)
                if c.gdrive:
                    self._gdrive(c, d, base)
                else:
                    self._hf(c, d, base)
                for name, digest in c.sha256.items():
                    if _sha256(d / name) != digest:
                        (d / name).unlink(missing_ok=True)
                        raise RuntimeError(f"{c.name}: {name} failed its SHA-256 check (download removed)")
                if not installed(self.models_dir, c):
                    raise RuntimeError(f"{c.name}: download finished but files are missing")
                base += c.size_bytes
                log.info("installed %s", c.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("paintbrush install failed: %s", exc)
            self.state["error"] = str(exc) or exc.__class__.__name__
        finally:
            self.state.update(running=False, component=None, finished_at=time.time())
            self._emit()

    def _gdrive(self, c: Component, d: Path, base: int) -> None:
        done = 0
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as client:
            for name, fid in c.gdrive.items():
                dest = d / name
                if dest.is_file() and (name not in c.sha256 or _sha256(dest) == c.sha256[name]):
                    done += dest.stat().st_size
                    continue
                tmp = dest.with_name(dest.name + ".partial")
                with client.stream("GET", GDRIVE_URL.format(id=fid)) as r:
                    r.raise_for_status()
                    if "text/html" in r.headers.get("content-type", ""):
                        raise RuntimeError(f"Google Drive refused the download of {name} (quota or removed file)")
                    last = 0.0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            if self._cancel.is_set():
                                raise RuntimeError("cancelled")
                            f.write(chunk)
                            done += len(chunk)
                            if time.time() - last > 0.5:
                                last = time.time()
                                self.state["done_bytes"] = base + done
                                self._emit()
                os.replace(tmp, dest)

    def _hf(self, c: Component, d: Path, base: int) -> None:
        from huggingface_hub import snapshot_download

        stop = threading.Event()

        def poll():
            while not stop.wait(1.0):
                self.state["done_bytes"] = base + min(_dir_bytes(d), c.size_bytes)
                self._emit()

        t = threading.Thread(target=poll, daemon=True)
        t.start()
        try:
            snapshot_download(c.hf_repo, revision=c.hf_revision or None, local_dir=str(d),
                              allow_patterns=list(c.hf_allow), token=self.hf_token() or None)
        finally:
            stop.set()
            t.join(2)
