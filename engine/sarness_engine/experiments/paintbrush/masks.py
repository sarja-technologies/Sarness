"""Semantic mask wire format, validation and conversion (no torch).

Wire format (JSON)::

    {"width": 768, "height": 512, "encoding": "deflate" | "raw", "data": "<base64>"}

``data`` is the row-major label map, one byte per pixel, each byte a material
index (see :mod:`.materials`). ``deflate`` is zlib-wrapped (what the browser's
``CompressionStream('deflate')`` produces); ``raw`` is uncompressed.
"""

from __future__ import annotations

import base64
import binascii
import io
import zlib
from typing import Any

import numpy as np
from PIL import Image

from . import materials as M

MIN_SIDE = 256
MAX_SIDE = 1024
MULTIPLE = 32  # SPADE upsamples a 1/32 latent five times


class MaskError(ValueError):
    pass


def check_size(width: int, height: int) -> None:
    for name, v in (("width", width), ("height", height)):
        if not isinstance(v, int) or v < MIN_SIDE or v > MAX_SIDE or v % MULTIPLE:
            raise MaskError(f"{name} must be a multiple of {MULTIPLE} between {MIN_SIDE} and {MAX_SIDE} (got {v})")


def decode(payload: dict[str, Any]) -> np.ndarray:
    """Returns an ``(H, W)`` uint8 label map of material indices, fully validated."""
    if not isinstance(payload, dict):
        raise MaskError("mask must be an object with width, height, encoding and data")
    try:
        width, height = int(payload["width"]), int(payload["height"])
    except (KeyError, TypeError, ValueError):
        raise MaskError("mask width and height are required") from None
    check_size(width, height)
    enc = payload.get("encoding", "deflate")
    try:
        blob = base64.b64decode(payload.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        raise MaskError("mask data is not valid base64") from None
    n = width * height
    if enc == "deflate":
        d = zlib.decompressobj()
        try:
            raw = d.decompress(blob, n + 1)  # bounded: refuses decompression bombs
        except zlib.error:
            raise MaskError("mask data is not valid deflate (zlib) data") from None
    elif enc == "raw":
        raw = blob
    else:
        raise MaskError("mask encoding must be 'deflate' or 'raw'")
    if len(raw) != n:
        raise MaskError(f"mask has {len(raw)} bytes, expected {width}×{height} = {n}")
    labels = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
    lo, hi = int(labels.min()), int(labels.max())
    if lo < 1 or hi > M.MAX_INDEX:
        raise MaskError(f"mask contains material index {lo if lo < 1 else hi}; valid indices are 1–{M.MAX_INDEX}")
    return labels


def encode(labels: np.ndarray, encoding: str = "deflate") -> dict[str, Any]:
    """Inverse of :func:`decode` (used by tests and tools)."""
    h, w = labels.shape
    raw = np.ascontiguousarray(labels, dtype=np.uint8).tobytes()
    data = zlib.compress(raw, 6) if encoding == "deflate" else raw
    return {"width": int(w), "height": int(h), "encoding": encoding, "data": base64.b64encode(data).decode()}


_COCO = np.array(M.coco_lut(), dtype=np.uint8)
_ADE = np.array(M.ade_lut(), dtype=np.uint8)
_COLOR = np.array(M.color_lut(), dtype=np.uint8)


def to_coco(labels: np.ndarray) -> np.ndarray:
    """COCO-Stuff class ids, exactly what the live SPADE model is fed."""
    return _COCO[labels]


def to_ade_rgb(labels: np.ndarray) -> np.ndarray:
    """ADE20K colour image, exactly what ControlNet-Seg is fed."""
    return _ADE[labels]


def to_color(labels: np.ndarray) -> np.ndarray:
    return _COLOR[labels]


def coverage(labels: np.ndarray) -> dict[str, float]:
    counts = np.bincount(labels.ravel(), minlength=M.MAX_INDEX + 1)
    total = float(labels.size)
    return {M.by_index(i).id: round(counts[i] / total, 4) for i in range(1, M.MAX_INDEX + 1) if counts[i]}


def build_prompt(labels: np.ndarray, guidance: str = "", min_share: float = 0.01) -> str:
    """Refine prompt: the user's guidance first, then the painted materials by area."""
    cov = coverage(labels)
    terms: list[str] = []
    for mid, share in sorted(cov.items(), key=lambda kv: -kv[1]):
        if share < min_share:
            continue
        term = M.MATERIALS[M.INDEX[mid] - 1].prompt
        if term not in terms:
            terms.append(term)
    scene = "a photograph of a natural landscape"
    if terms:
        scene += " with " + (", ".join(terms[:-1]) + " and " + terms[-1] if len(terms) > 1 else terms[0])
    parts = [p for p in ((guidance or "").strip(), scene, "highly detailed, natural light, sharp focus, 8k") if p]
    return ", ".join(parts)


NEGATIVE_PROMPT = ("painting, illustration, drawing, cartoon, anime, cgi, render, oversaturated, blurry, lowres, "
                   "jpeg artifacts, text, watermark, signature, frame, border")


def png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def export_png(labels: np.ndarray, fmt: str) -> bytes:
    """``coco``: 8-bit grey COCO-Stuff ids · ``ade``: ADE20K colours · ``color``: interface colours ·
    ``index``: 8-bit grey material indices."""
    if fmt == "coco":
        return png_bytes(to_coco(labels))
    if fmt == "ade":
        return png_bytes(to_ade_rgb(labels))
    if fmt == "color":
        return png_bytes(to_color(labels))
    if fmt == "index":
        return png_bytes(labels.astype(np.uint8))
    raise MaskError("format must be coco, ade, color or index")
