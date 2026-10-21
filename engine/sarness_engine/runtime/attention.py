"""Attention over padded sequences without a dense mask.

HunyuanVideo 1.5 hides padded text tokens with a dense (sequence × sequence) boolean mask: the outer product of
a per-token padding vector. A dense mask rules out flash attention, so at 848×480 every step pushed ~50k tokens
through the masked kernel (2.5 min per step on GB10, 75 GB in use). Attending over only the valid tokens, unmasked,
gives the same result for every valid token; padded positions get zeros, which nothing reads because they are
masked out as keys everywhere.
"""

from __future__ import annotations

from typing import Any, Callable

import torch


def padding_vector(mask: Any, seq: int) -> torch.Tensor | None:
    """The (batch, seq) validity vector behind an outer-product padding mask, or None for any other mask."""
    if not isinstance(mask, torch.Tensor) or mask.dtype != torch.bool or mask.dim() != 4 or mask.shape[1] != 1:
        return None
    if mask.shape[-1] != seq or mask.shape[-2] != seq:
        return None
    m = mask[:, 0]
    valid = m.any(dim=-1)
    # m lies inside rows × columns that have any True; with both equal to `valid` and |valid|² entries set,
    # m is exactly valid ⊗ valid (checked without building a second sequence × sequence tensor).
    if not torch.equal(m.any(dim=-2), valid):
        return None
    n = valid.sum(dim=-1)
    if not torch.equal(m.sum(dim=(-2, -1)), n * n):
        return None
    return valid


def packed(fn: Callable[..., torch.Tensor]) -> Callable[..., torch.Tensor]:
    """Wraps an attention function taking (batch, seq, heads, dim) tensors so that padding masks become packing."""
    if getattr(fn, "_sarness_packed", False):
        return fn

    def attention(query, key, value, attn_mask=None, **kw):
        seq = query.shape[1]
        valid = padding_vector(attn_mask, seq) if key.shape[1] == seq else None
        if valid is None:
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
