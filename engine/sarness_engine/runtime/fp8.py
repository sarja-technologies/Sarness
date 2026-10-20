"""FP8 linear layers and a low-overhead safetensors reader for very large models.

Weights are float8 e4m3 with one float32 scale per output row (weight = qdata * scale), dequantized
to the activation dtype for each matmul: half the memory of bf16, activations kept at full precision.
FP8 tensor-core matmuls were measured on GB10 (torch 2.14, 8192x5376 @ 5376x14336): bf16 12.4 ms,
dequantize + bf16 15.1 ms, torch._scaled_mm with per-row scales 23.9 ms (no fast kernel for sm_121),
and per-tensor scales 6.2 ms but ~13 ms once activations are quantized and the per-row scales applied.
The state-dict layout (``<name>._weight_qdata`` / ``<name>._weight_scale``) is the one torchao writes
for a serialized Float8Tensor, so pre-quantized checkpoints load without torchao.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Callable

import torch
from torch import nn
from torch.nn import functional as F

E4M3_MAX = 448.0


class Fp8Linear(nn.Module):
    """Drop-in replacement for nn.Linear holding an FP8 weight with per-row scales."""

    def __init__(self, in_features: int, out_features: int, bias: bool = True, device: Any = None,
                 dtype: torch.dtype = torch.bfloat16):
        super().__init__()
        self.in_features, self.out_features = in_features, out_features
        # Registered first and always real: diffusers and transformers infer a module's compute dtype
        # from its first floating tensor, which must not be the float8 weight.
        self.register_buffer("_dtype_anchor", torch.empty(0, dtype=dtype), persistent=False)
        self.register_buffer("_weight_qdata", torch.empty(out_features, in_features, dtype=torch.float8_e4m3fn,
                                                          device=device))
        self.register_buffer("_weight_scale", torch.empty(out_features, 1, dtype=torch.float32, device=device))
        self.bias = nn.Parameter(torch.empty(out_features, dtype=dtype, device=device), requires_grad=False) if bias else None

    @classmethod
    def from_linear(cls, lin: nn.Linear) -> "Fp8Linear":
        """Quantizes an existing layer (per output row, absmax), on the layer's device."""
        w = lin.weight.detach()
        scale = (w.abs().amax(dim=1, keepdim=True).float() / E4M3_MAX).clamp_(min=1e-12)
        m = cls(lin.in_features, lin.out_features, bias=lin.bias is not None, device=w.device, dtype=w.dtype)
        m._weight_qdata = (w.float() / scale).clamp_(-E4M3_MAX, E4M3_MAX).to(torch.float8_e4m3fn)
        m._weight_scale = scale
        if lin.bias is not None:
            m.bias = nn.Parameter(lin.bias.detach(), requires_grad=False)
        m._dtype_anchor = m._dtype_anchor.to(w.device)
        return m

    def extra_repr(self) -> str:
        return f"in_features={self.in_features}, out_features={self.out_features}, bias={self.bias is not None}, fp8"

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        w = self._weight_qdata.to(x.dtype) * self._weight_scale.to(x.dtype)
        return F.linear(x, w, None if self.bias is None else self.bias.to(x.dtype))


def set_submodule(root: nn.Module, name: str, module: nn.Module) -> None:
    parent, _, leaf = name.rpartition(".")
    setattr(root.get_submodule(parent) if parent else root, leaf, module)


def quantize_linears(root: nn.Module, device: Any, include: Callable[[str], bool] = lambda n: True) -> int:
    """Replaces every nn.Linear under ``root`` (that ``include`` accepts) with an Fp8Linear, one layer
    at a time on ``device``, so the full-precision copy never exists twice."""
    names = [n for n, m in root.named_modules() if isinstance(m, nn.Linear) and include(n)]
    for n in names:
        set_submodule(root, n, Fp8Linear.from_linear(root.get_submodule(n).to(device)))
    return len(names)


# ------------------------------------------------------------------- reading
_DTYPES = {"F8_E4M3": torch.float8_e4m3fn, "F8_E5M2": torch.float8_e5m2, "BF16": torch.bfloat16,
           "F16": torch.float16, "F32": torch.float32, "F64": torch.float64, "I64": torch.int64,
           "I32": torch.int32, "I16": torch.int16, "I8": torch.int8, "U8": torch.uint8, "BOOL": torch.bool}


def safetensors_header(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """(tensor table, __metadata__) of a safetensors file."""
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
    return header, header.pop("__metadata__", None) or {}


def read_safetensors(path: Path, skip: Callable[[str], bool] = lambda k: False) -> dict[str, torch.Tensor]:
    """Reads tensors with plain sequential reads rather than mmap (see runtime.models.fast_load: on
    unified-memory GPUs mmap'd weights reach the GPU many times slower), one tensor buffer at a time,
    so the peak is the kept tensors rather than the file plus a copy of it."""
    with open(path, "rb", buffering=0) as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
        header.pop("__metadata__", None)
        base = 8 + n
        out: dict[str, torch.Tensor] = {}
        for name, info in sorted(header.items(), key=lambda kv: kv[1]["data_offsets"][0]):
            if skip(name):
                continue
            a, b = info["data_offsets"]
            buf = torch.empty(b - a, dtype=torch.uint8)
            view = memoryview(buf.numpy())
            f.seek(base + a)
            got = 0
            while got < b - a:
                r = f.readinto(view[got:])
                if not r:
                    raise OSError(f"{path} is truncated at {name}")
                got += r
            out[name] = buf.view(_DTYPES[info["dtype"]]).reshape(info["shape"])
    return out
