"""SNP-CBOR: the strict CBOR profile of SNP/1 (docs/SNP.md section 4.2).

Hand-written on purpose. Every declared length is validated against the octets that
remain *before* anything is allocated, so a few bytes can never request a large buffer.
Accepted: unsigned/negative integers within int64, byte and text strings, arrays, maps with
unique text keys, ``false``/``true``/``null`` and finite floats. Rejected: tags, indefinite
lengths, non-shortest integer/length encodings, ``undefined`` and other simple values,
NaN/Infinity, invalid UTF-8, duplicate keys, nesting beyond 32, more than 100 000 items and
trailing octets.
"""

from __future__ import annotations

import math
import struct
from typing import Any

MAX_DEPTH = 32
MAX_ITEMS = 100_000
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1


class CborError(ValueError):
    """The payload violates the SNP-CBOR profile."""


# --------------------------------------------------------------------------- encoding
def _head(major: int, n: int) -> bytes:
    m = major << 5
    if n < 24:
        return bytes((m | n,))
    if n < 0x100:
        return bytes((m | 24, n))
    if n < 0x10000:
        return struct.pack(">BH", m | 25, n)
    if n < 0x100000000:
        return struct.pack(">BI", m | 26, n)
    return struct.pack(">BQ", m | 27, n)


def _encode_float(x: float) -> bytes:
    if math.isnan(x) or math.isinf(x):
        raise CborError("NaN and Infinity cannot be encoded")
    for fmt, head in ((">e", 0xF9), (">f", 0xFA)):
        try:
            packed = struct.pack(fmt, x)
        except OverflowError:
            continue
        back = struct.unpack(fmt, packed)[0]
        if back == x and math.copysign(1.0, back) == math.copysign(1.0, x):
            return bytes((head,)) + packed
    return b"\xfb" + struct.pack(">d", x)


def _enc(obj: Any, out: list[bytes], depth: int) -> None:
    if depth > MAX_DEPTH and isinstance(obj, (list, tuple, dict)):
        raise CborError("nesting too deep")
    if obj is None:
        out.append(b"\xf6")
    elif obj is True:
        out.append(b"\xf5")
    elif obj is False:
        out.append(b"\xf4")
    elif isinstance(obj, int):
        if not INT_MIN <= obj <= INT_MAX:
            raise CborError("integer outside int64")
        out.append(_head(0, obj) if obj >= 0 else _head(1, -1 - obj))
    elif isinstance(obj, float):
        out.append(_encode_float(obj))
    elif isinstance(obj, str):
        raw = obj.encode("utf-8")
        out.append(_head(3, len(raw)))
        out.append(raw)
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        raw = bytes(obj)
        out.append(_head(2, len(raw)))
        out.append(raw)
    elif isinstance(obj, (list, tuple)):
        out.append(_head(4, len(obj)))
        for item in obj:
            _enc(item, out, depth + 1)
    elif isinstance(obj, dict):
        pairs = []
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CborError("map keys must be text")
            kb = k.encode("utf-8")
            pairs.append((_head(3, len(kb)) + kb, v))
        pairs.sort(key=lambda p: p[0])
        out.append(_head(5, len(pairs)))
        for kb, v in pairs:
            out.append(kb)
            _enc(v, out, depth + 1)
    else:
        raise CborError(f"cannot encode {type(obj).__name__}")


def dumps(obj: Any) -> bytes:
    out: list[bytes] = []
    _enc(obj, out, 1)
    return b"".join(out)


# --------------------------------------------------------------------------- decoding
class _Decoder:
    __slots__ = ("buf", "pos", "items", "max_depth", "max_items")

    def __init__(self, buf: bytes, max_depth: int, max_items: int):
        self.buf = buf
        self.pos = 0
        self.items = 0
        self.max_depth = max_depth
        self.max_items = max_items

    def _arg(self, ai: int) -> int:
        if ai < 24:
            return ai
        size = {24: 1, 25: 2, 26: 4, 27: 8}.get(ai)
        if size is None:
            # 28-30 are reserved, 31 is the indefinite-length marker.
            raise CborError("indefinite or reserved additional information")
        end = self.pos + size
        if end > len(self.buf):
            raise CborError("truncated argument")
        n = int.from_bytes(self.buf[self.pos:end], "big")
        self.pos = end
        if n < (24, 0x100, 0x10000, 0x100000000)[(24, 25, 26, 27).index(ai)]:
            raise CborError("non-shortest encoding")
        return n

    def _take(self, n: int) -> bytes:
        # n is compared with what is left before the slice is made.
        if n > len(self.buf) - self.pos:
            raise CborError("declared length exceeds payload")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def item(self, depth: int) -> Any:
        self.items += 1
        if self.items > self.max_items:
            raise CborError("too many data items")
        if self.pos >= len(self.buf):
            raise CborError("truncated payload")
        ib = self.buf[self.pos]
        self.pos += 1
        major, ai = ib >> 5, ib & 31
        if major == 7:
            return self._simple(ai)
        if major == 6:
            raise CborError("tags are not permitted")
        n = self._arg(ai)
        if major == 0:
            if n > INT_MAX:
                raise CborError("integer outside int64")
            return n
        if major == 1:
            if n > INT_MAX:
                raise CborError("integer outside int64")
            return -1 - n
        if major == 2:
            return self._take(n)
        if major == 3:
            raw = self._take(n)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise CborError("invalid UTF-8") from None
        if major >= 4 and depth > self.max_depth:
            raise CborError("nesting too deep")
        if major == 4:
            if n > len(self.buf) - self.pos:
                raise CborError("array length exceeds payload")
            return [self.item(depth + 1) for _ in range(n)]
        # major == 5
        if 2 * n > len(self.buf) - self.pos:
            raise CborError("map length exceeds payload")
        out: dict[str, Any] = {}
        for _ in range(n):
            if self.pos >= len(self.buf):
                raise CborError("truncated payload")
            if self.buf[self.pos] >> 5 != 3:
                raise CborError("map keys must be text strings")
            key = self.item(depth + 1)
            if key in out:
                raise CborError("duplicate map key")
            out[key] = self.item(depth + 1)
        return out

    def _simple(self, ai: int) -> Any:
        if ai == 20:
            return False
        if ai == 21:
            return True
        if ai == 22:
            return None
        if ai in (25, 26, 27):
            size = {25: 2, 26: 4, 27: 8}[ai]
            end = self.pos + size
            if end > len(self.buf):
                raise CborError("truncated float")
            x = struct.unpack({25: ">e", 26: ">f", 27: ">d"}[ai], self.buf[self.pos:end])[0]
            self.pos = end
            if math.isnan(x) or math.isinf(x):
                raise CborError("NaN and Infinity are not permitted")
            return x
        raise CborError("simple value not permitted")


def loads(data: bytes, *, max_depth: int = MAX_DEPTH, max_items: int = MAX_ITEMS) -> Any:
    """Decodes exactly one SNP-CBOR item; raises :class:`CborError` on any violation."""
    d = _Decoder(bytes(data), max_depth, max_items)
    value = d.item(1)
    if d.pos != len(d.buf):
        raise CborError("trailing octets after the top-level item")
    return value
