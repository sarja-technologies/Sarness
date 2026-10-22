"""Attention over padded sequences without a dense mask.

HunyuanVideo 1.5 hides padded text tokens with a dense (sequence × sequence) boolean mask: the outer product of
a per-token padding vector. A dense mask rules out flash attention, so at 848×480 every step pushed ~50k tokens
through the masked kernel (2.5 min per step on GB10, 75 GB in use). Attending over only the valid tokens, unmasked,
gives the same result for every valid token; padded positions get zeros, which nothing reads because they are
masked out as keys everywhere.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import torch

log = logging.getLogger("sarness.attention")


def padding_vector(mask: Any, seq: int) -> torch.Tensor | None:
    """The (batch, seq) validity vector behind an outer-product padding mask, or None for any other mask."""
    if not isinstance(mask, torch.Tensor) or mask.dtype != torch.bool or mask.dim() != 4 or mask.shape[1] != 1:
        return None
    if mask.shape[-1] != seq or mask.shape[-2] != seq:
        return None
    m = mask[:, 0]
    # For m = v ⊗ v the diagonal is v itself. The wrapper is installed only where every mask is built that way
    # (HunyuanVideo 1.5), so a sample of rows, including the first padded one, guards against a different mask
    # (a causal mask fails at row 0) without passing over the whole mask on every call: a full check costs 270 ms
    # per call at 50k tokens, and a sum over it would upcast the booleans to 94 GB of int64.
    valid = m.diagonal(dim1=-2, dim2=-1)
    rows = set(range(0, seq, max(1, seq // 16))) | {seq - 1}
    for b in range(m.shape[0]):
        pad = (~valid[b]).nonzero()
        if pad.numel():
            rows.add(int(pad[0]))
    rows = torch.tensor(sorted(rows), device=m.device)
    if not torch.equal(m[:, rows], valid[:, rows, None] & valid[:, None, :]):
        return None
    return valid.clone()


def packed(fn: Callable[..., torch.Tensor]) -> Callable[..., torch.Tensor]:
    """Wraps an attention function taking (batch, seq, heads, dim) tensors so that padding masks become packing."""
    if getattr(fn, "_sarness_packed", False):
        return fn

    def attention(query, key, value, attn_mask=None, **kw):
        seq = query.shape[1]
        valid = padding_vector(attn_mask, seq) if key.shape[1] == seq else None
        if valid is None:
            if attn_mask is not None and not attention._warned:
                attention._warned = True
                log.warning("attention mask %s is not a padding mask; using the masked kernel (slow)",
                            tuple(getattr(attn_mask, "shape", ())))
            return fn(query, key, value, attn_mask=attn_mask, **kw)
        if bool(valid.all()):
            return fn(query, key, value, attn_mask=None, **kw)
        out = query.new_zeros((*query.shape[:3], value.shape[-1]))
        for b in range(query.shape[0]):
            idx = valid[b].nonzero().squeeze(1)
            if idx.numel():
                out[b:b + 1, idx] = fn(query[b:b + 1, idx], key[b:b + 1, idx], value[b:b + 1, idx], attn_mask=None,
                                       **kw).to(out.dtype)
        return out

    attention._sarness_packed = True
    attention._warned = False
    return attention


def pack_hunyuan_video15_attention() -> bool:
    """Patches diffusers' HunyuanVideo 1.5 transformer (process-wide, idempotent). False when it cannot."""
    try:
        from diffusers.models.transformers import transformer_hunyuan_video15 as mod
    except ImportError:
        return False
    if not hasattr(mod, "dispatch_attention_fn"):
        return False
    mod.dispatch_attention_fn = packed(mod.dispatch_attention_fn)
    return True
