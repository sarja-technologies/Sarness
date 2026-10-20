"""llama.cpp ``llama-server`` lifecycle management and runtime installation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..hardware import arch, gpus

log = logging.getLogger("sarness.llama")

RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
REPO_URL = "https://github.com/ggml-org/llama.cpp"
# Prebuilt release archives exist only for x86-64; other machines build from source.
PREBUILT = {"cuda-12.8": "ubuntu-cuda-12.8-x64", "vulkan": "ubuntu-vulkan-x64", "cpu": "ubuntu-x64"}
VARIANTS = (*PREBUILT, "cuda-src", "cpu-src")
BUILD_TARGETS = ("llama-server", "llama-bench", "llama-cli")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaRuntime:
    """Finds or installs a llama-server binary."""

    def __init__(self, bin_dir: Path):
        self.root = bin_dir / "llama"

    def binary(self) -> Path | None:
        if self.root.exists():
            for p in sorted(self.root.rglob("llama-server")):
                if os.access(p, os.X_OK):
                    return p
        found = shutil.which("llama-server")
        return Path(found) if found else None

    def version(self) -> str | None:
        b = self.binary()
        if not b:
            return None
        marker = self.root / "VERSION"
        if marker.exists():
            return marker.read_text().strip()
        try:
            out = subprocess.run([str(b), "--version"], capture_output=True, text=True, timeout=10,
                                 env=self._env(b))
            return (out.stdout + out.stderr).strip().splitlines()[0][:120]
        except Exception:
            return "unknown"

    def _env(self, b: Path) -> dict[str, str]:
        env = dict(os.environ)
        libdirs = {str(b.parent)}
        if self.root.exists():
            libdirs |= {str(p.parent) for p in self.root.rglob("libcudart.so*")}
        env["LD_LIBRARY_PATH"] = ":".join([*libdirs, env.get("LD_LIBRARY_PATH", "")]).strip(":")
        return env

    def env(self) -> dict[str, str]:
        b = self.binary()
        return self._env(b) if b else dict(os.environ)

    @staticmethod
    def choose_variant() -> str:
        if arch() != "x86_64":
            return "cuda-src" if gpus() and find_nvcc() else "cpu-src"
        if gpus():
            return "cuda-12.8"
        try:
            import ctypes

            ctypes.CDLL("libvulkan.so.1")
            return "vulkan"
        except OSError:
            return "cpu"

    def install(self, progress=None, variant: str | None = None, ref: str | None = None) -> str:
        """Installs llama.cpp for this machine: the latest official x86-64 release (CUDA, Vulkan
        or CPU), or a source build (`*-src`, e.g. ARM64 with CUDA 13 on DGX Spark)."""
        variant = variant or self.choose_variant()
        if variant not in VARIANTS:
            raise ValueError(f"unknown llama.cpp variant {variant!r}")
        if variant.endswith("-src"):
            return self.build(progress, cuda=variant == "cuda-src", ref=ref)
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as c:
            releases = c.get(RELEASES_API, headers={"Accept": "application/vnd.github+json"}).json()
            suffix = PREBUILT[variant]
            # The newest release that actually ships prebuilt Linux binaries for this variant.
            for rel in releases:
                tag = rel["tag_name"]
                # llama.cpp ships its numbered builds (bNNNN) flagged as pre-releases.
                if rel.get("draft") or (rel.get("prerelease") and not re.fullmatch(r"b\d+", tag)):
                    continue
                wanted = [f"llama-{tag}-bin-{suffix}.tar.gz"]
                if variant.startswith("cuda"):
                    wanted.append(f"cudart-llama-{tag}-bin-{suffix}.tar.gz")
                assets = {a["name"]: a for a in rel.get("assets", [])}
                if all(w in assets for w in wanted):
                    break
            else:
                raise RuntimeError(f"no recent llama.cpp release provides {suffix} binaries")
            staging = self.root.parent / "llama.staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            total = sum(assets[w]["size"] for w in wanted)
            done = 0
            for w in wanted:
                dest = staging / w
                with c.stream("GET", assets[w]["browser_download_url"]) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                with tarfile.open(dest) as tf:
                    tf.extractall(staging / "x", filter="data")
                dest.unlink()
            shutil.rmtree(self.root, ignore_errors=True)
            (staging / "x").rename(self.root)
            shutil.rmtree(staging, ignore_errors=True)
            (self.root / "VERSION").write_text(f"{tag} ({variant})")
            b = self.binary()
            if not b:
                raise RuntimeError("llama-server not found in the downloaded release")
            log.info("installed llama.cpp %s (%s) at %s", tag, variant, b)
            return f"{tag} ({variant})"

    def build(self, progress=None, *, cuda: bool, ref: str | None = None, jobs: int | None = None) -> str:
        """Builds llama.cpp from a release tag (latest by default) with CMake.

        CUDA builds target the compute capability of the installed GPU (e.g. 12.1 → `121a-real`
        on GB10), so the kernels are native rather than JIT-compiled from PTX.
        """
        for tool in ("git", "cmake"):
            if not shutil.which(tool):
                raise RuntimeError(f"building llama.cpp needs `{tool}` on PATH")
        nvcc = find_nvcc() if cuda else None
        if cuda and not nvcc:
            raise RuntimeError("building llama.cpp with CUDA needs the CUDA toolkit (nvcc not found)")
        if not ref:
            with httpx.Client(follow_redirects=True, timeout=30) as c:
                rel = c.get(RELEASES_API.split("?")[0] + "/latest",
                            headers={"Accept": "application/vnd.github+json"}).json()
            ref = rel["tag_name"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", ref):
            raise ValueError(f"invalid llama.cpp ref {ref!r}")
        staging = self.root.parent / "llama.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        logf = staging / "build.log"
        steps: list[list[str]] = [["git", "clone", "--depth", "1", "--branch", ref, REPO_URL, str(staging / "src")]]
        configure = ["cmake", "-S", str(staging / "src"), "-B", str(staging / "build"),
                     "-DCMAKE_BUILD_TYPE=Release", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
                     "-DLLAMA_CURL=OFF", "-DGGML_NATIVE=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN",
                     "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON"]
        if shutil.which("ninja"):
            configure += ["-G", "Ninja"]
        env = dict(os.environ)
        if cuda:
            cc = cuda_architecture()
            configure += ["-DGGML_CUDA=ON", f"-DCMAKE_CUDA_COMPILER={nvcc}"]
            if cc:
                configure.append(f"-DCMAKE_CUDA_ARCHITECTURES={cc}")
            env["PATH"] = f"{Path(nvcc).parent}:{env.get('PATH', '')}"
        steps.append(configure)
        steps.append(["cmake", "--build", str(staging / "build"), "--config", "Release",
                      "-j", str(jobs or os.cpu_count() or 4), "--target", *BUILD_TARGETS])
        with open(logf, "wb") as lf:
            for n, cmd in enumerate(steps):
                log.info("llama.cpp build: %s", " ".join(cmd[:4]))
                if progress:
                    progress(n, len(steps))
                r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
                if r.returncode != 0:
                    raise RuntimeError(f"llama.cpp build step failed ({cmd[0]} {cmd[1]}); see {logf}")
        out = staging / "x" / f"llama-{ref}"
        out.mkdir(parents=True)
        for f in (staging / "build" / "bin").iterdir():
            if f.is_file():
                shutil.copy2(f, out / f.name)
        variant = "cuda-src" if cuda else "cpu-src"
        shutil.rmtree(self.root, ignore_errors=True)
        (staging / "x").rename(self.root)
        shutil.copy2(logf, self.root / "build.log")
        shutil.rmtree(staging, ignore_errors=True)
        label = f"{ref} ({variant}{', sm ' + cuda_architecture() if cuda else ''})"
        (self.root / "VERSION").write_text(label)
        if progress:
            progress(len(steps), len(steps))
        if not self.binary():
            raise RuntimeError("llama-server was not produced by the build")
        log.info("built llama.cpp %s at %s", label, self.binary())
        return label


def find_nvcc() -> str | None:
    """The CUDA compiler, also when /usr/local/cuda/bin is not on PATH (the usual case for services)."""
    found = shutil.which("nvcc")
    if found:
        return found
    for cand in [os.environ.get("CUDA_HOME", ""), "/usr/local/cuda", *sorted(map(str, Path("/usr/local").glob("cuda-*")),
                                                                         reverse=True)]:
        if cand and os.access(Path(cand) / "bin" / "nvcc", os.X_OK):
            return str(Path(cand) / "bin" / "nvcc")
    return None


def cuda_architecture() -> str | None:
    """CMake CUDA architecture for GPU 0: `121a-real` for compute capability 12.1. The `a` suffix
    enables architecture-specific instructions (Blackwell block-scaled MMA used by MXFP4/NVFP4)."""
    g = gpus()
    cc = g[0].get("compute_capability") if g else None
    if not cc:
        return None
    major, minor = cc.split(".")
    num = int(major) * 10 + int(minor)
    return f"{num}a-real" if num >= 90 else f"{num}-real"


class LlamaServer:
    """One managed llama-server process serving one model."""

    def __init__(self, runtime: LlamaRuntime, logs_dir: Path):
        self.runtime = runtime
        self.logs_dir = logs_dir
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.model: dict[str, Any] | None = None
        self.options: dict[str, Any] = {}
        self.state = "stopped"  # stopped | loading | ready | error
        self.error: str | None = None
        self.active_streams = 0
        self.last_used = time.time()
        self.loaded_at: float | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def status(self) -> dict[str, Any]:
        alive = self.proc is not None and self.proc.poll() is None
        if self.state == "ready" and not alive:
            self.state, self.error = "error", "llama-server exited unexpectedly"
        return {
            "state": self.state, "error": self.error,
            "model_id": self.model["id"] if self.model else None,
            "model_name": self.model.get("name") if self.model else None,
            "vision": bool(self.options.get("mmproj")),
            "ctx": self.options.get("ctx"), "gpu_layers": self.options.get("gpu_layers"),
            "pid": self.proc.pid if alive else None, "active_streams": self.active_streams,
            "loaded_at": self.loaded_at, "last_used": self.last_used,
            "runtime": self.runtime.version(),
            "vram_estimate_mb": self.model.get("vram_mb") if self.model else None,
        }

    @property
    def busy(self) -> bool:
        return self.active_streams > 0 or self.state == "loading"

    def load(self, model: dict[str, Any], *, ctx: int, gpu_layers: int, flash_attn: bool, timeout: float = 300,
             fit_target_mb: int = 1024) -> None:
        with self._lock:
            b = self.runtime.binary()
            if not b:
                raise RuntimeError("llama.cpp runtime is not installed (Models → Runtimes → Install llama.cpp)")
            if self.model and self.model["id"] == model["id"] and self.state == "ready" and self.options.get("ctx") == ctx \
                    and self.options.get("gpu_layers") == gpu_layers:
                return
            self._stop_locked()
            self.port = _free_port()
            mmproj = (model.get("meta") or {}).get("mmproj")
            args = [str(b), "-m", str(model["path"]), "--host", "127.0.0.1", "--port", str(self.port),
                    "-c", str(ctx), "--jinja", "--metrics", "--no-webui",
                    "-fa", "on" if flash_attn else "off", "--parallel", "1"]
            if gpu_layers >= 999:
                # Let llama.cpp place as many layers as fit in *free* VRAM (partial CPU
                # offload instead of an out-of-memory crash), keeping some headroom.
                args += ["--fit", "on", "--fit-target", str(fit_target_mb)]
            else:
                args += ["-ngl", str(gpu_layers)]
            if mmproj:
                args += ["--mmproj", mmproj]
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            logf = open(self.logs_dir / "llama-server.log", "ab")
            log.info("starting llama-server for %s (ctx=%d, ngl=%d)", model["id"], ctx, gpu_layers)
            self.state, self.error = "loading", None
            self.model = model
            self.options = {"ctx": ctx, "gpu_layers": gpu_layers, "flash_attn": flash_attn, "mmproj": mmproj}
            self.proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, env=self.runtime.env(),
                                         start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.state = "error"
                self.error = _tail(self.logs_dir / "llama-server.log") or f"llama-server exited ({self.proc.returncode})"
                raise RuntimeError(self.error)
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=2)
                if r.status_code == 200:
                    self.state = "ready"
                    self.loaded_at = self.last_used = time.time()
                    log.info("llama-server ready on port %d", self.port)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.unload()
        self.state, self.error = "error", "timed out waiting for llama-server to load the model"
        raise RuntimeError(self.error)

    def _stop_locked(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None
        self.state = "stopped"
        self.model = None
        self.loaded_at = None

    def unload(self) -> None:
        with self._lock:
            self._stop_locked()
            log.info("llama-server stopped")

    def tick(self, idle_minutes: float) -> None:
        if self.state == "ready" and not self.busy and idle_minutes > 0 and time.time() - self.last_used > idle_minutes * 60:
            log.info("unloading idle LLM %s", self.model and self.model["id"])
            self.unload()

    async def stream_chat(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async for chunk in stream_openai(self.base_url + "/v1", None, body):
            yield chunk


async def stream_openai(base_url: str, api_key: str | None, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Streams chat completion chunks from any OpenAI-compatible endpoint."""
    import json

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {**body, "stream": True}
    body.setdefault("stream_options", {"include_usage": True})
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=600)) as c:
        async with c.stream("POST", base_url.rstrip("/") + "/chat/completions", json=body, headers=headers) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode("utf-8", "replace")[:800]
                raise RuntimeError(f"model server returned HTTP {r.status_code}: {text}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
                await asyncio.sleep(0)


def _tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()[-n:]
        errs = [l for l in lines if "error" in l.lower() or "failed" in l.lower()]
        return "; ".join(errs[-3:]) if errs else ""
    except OSError:
        return ""
