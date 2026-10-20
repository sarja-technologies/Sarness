"""Hardware and runtime introspection. Never imports torch (that would allocate VRAM)."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger("sarness.hardware")

try:
    import pynvml  # provided by nvidia-ml-py

    pynvml.nvmlInit()
    _NVML = True
except Exception:  # pragma: no cover - machines without NVIDIA driver
    pynvml = None
    _NVML = False


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def arch() -> str:
    """Normalised CPU architecture: x86_64 or aarch64."""
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


@lru_cache(maxsize=1)
def cpu_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    # ARM kernels do not report "model name"; lscpu decodes the core types (e.g. big.LITTLE).
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"}).stdout
        names = list(dict.fromkeys(l.split(":", 1)[1].strip() for l in out.splitlines()
                                   if l.strip().startswith("Model name:")))
        if names:
            return " + ".join(names)
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or platform.machine() or "Unknown CPU"


def _unified_memory_mb() -> tuple[int, int, int]:
    """(total, used, free) of system RAM, which a unified-memory GPU (GB10, Jetson) allocates from."""
    vm = psutil.virtual_memory()
    mb = 1024 * 1024
    return vm.total // mb, (vm.total - vm.available) // mb, vm.available // mb


def gpus() -> list[dict[str, Any]]:
    if not _NVML:
        return []
    out = []
    try:
        driver = _s(pynvml.nvmlSystemGetDriverVersion())
        try:
            cuda_drv = pynvml.nvmlSystemGetCudaDriverVersion_v2()
            cuda_driver = f"{cuda_drv // 1000}.{(cuda_drv % 1000) // 10}"
        except Exception:
            cuda_driver = None
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            # Unified-memory GPUs (DGX Spark GB10) answer NOT_SUPPORTED here: their memory is
            # the system RAM, so report that instead of dropping the device.
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                total_mb, used_mb, free_mb = (mem.total // (1024 * 1024), mem.used // (1024 * 1024),
                                              mem.free // (1024 * 1024))
                unified = False
            except Exception:
                total_mb, used_mb, free_mb = _unified_memory_mb()
                unified = True
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            except Exception:
                util = None
            try:
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(h)
                compute_capability = f"{major}.{minor}"
            except Exception:
                compute_capability = None
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                power_limit = pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
            except Exception:
                power = power_limit = None
            procs = []
            try:
                for p in pynvml.nvmlDeviceGetComputeRunningProcesses(h):
                    procs.append({"pid": p.pid, "used_mb": (p.usedGpuMemory or 0) // (1024 * 1024)})
            except Exception:
                pass
            out.append({
                "index": i,
                "name": _s(pynvml.nvmlDeviceGetName(h)),
                "vram_total_mb": total_mb,
                "vram_used_mb": used_mb,
                "vram_free_mb": free_mb,
                "unified_memory": unified,
                "compute_capability": compute_capability,
                "utilization_pct": util,
                "temperature_c": temp,
                "power_w": power,
                "power_limit_w": power_limit,
                "driver_version": driver,
                "cuda_driver_version": cuda_driver,
                "compute_processes": procs,
            })
    except Exception as exc:  # pragma: no cover
        log.warning("NVML query failed: %s", exc)
    return out


def vram_free_mb(index: int = 0) -> int | None:
    g = gpus()
    return g[index]["vram_free_mb"] if len(g) > index else None


def vram_total_mb(index: int = 0) -> int | None:
    g = gpus()
    return g[index]["vram_total_mb"] if len(g) > index else None


def system_snapshot(data_dir: Path) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(data_dir)
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": arch(),
        "distro": _distro(),
        "python": platform.python_version(),
        "cpu": {
            "name": cpu_name(),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "utilization_pct": psutil.cpu_percent(interval=None),
            "load_avg": list(load),
        },
        "memory": {
            "total_mb": vm.total // (1024 * 1024),
            "used_mb": (vm.total - vm.available) // (1024 * 1024),
            "available_mb": vm.available // (1024 * 1024),
        },
        "storage": {
            "path": str(data_dir),
            "total_gb": round(du.total / 1e9, 1),
            "used_gb": round(du.used / 1e9, 1),
            "free_gb": round(du.free / 1e9, 1),
        },
        "gpus": gpus(),
        "uptime_s": time.time() - psutil.boot_time(),
    }


@lru_cache(maxsize=1)
def _distro() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def disk_free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free
