"""Independent reference client for SNP/1, written from docs/SNP.md only.

It deliberately shares no code with ``sarness_engine.snp`` (own CBOR, own framing, own
handshake) so that the server tests exercise the specification, not the server's own helpers.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import ssl
import struct
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization

# Frame types (spec 4.1)
HELLO, HELLO_OK, AUTH, AUTH_OK, PING, PONG, GOAWAY = 1, 2, 3, 4, 5, 6, 7
OPEN, MSG, DATA, END, ERROR, CANCEL, WINDOW = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16


# --------------------------------------------------------------------------- CBOR (minimal, independent)
def _hd(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | ai]) + n.to_bytes(size, "big")
    raise ValueError("too large")


def cenc(o: Any) -> bytes:
    if o is None:
        return b"\xf6"
    if o is True:
        return b"\xf5"
    if o is False:
        return b"\xf4"
    if isinstance(o, int):
        return _hd(0, o) if o >= 0 else _hd(1, -1 - o)
    if isinstance(o, float):
        return b"\xfb" + struct.pack(">d", o)
    if isinstance(o, str):
        b = o.encode()
        return _hd(3, len(b)) + b
    if isinstance(o, (bytes, bytearray)):
        return _hd(2, len(o)) + bytes(o)
    if isinstance(o, (list, tuple)):
        return _hd(4, len(o)) + b"".join(cenc(x) for x in o)
    if isinstance(o, dict):
        return _hd(5, len(o)) + b"".join(cenc(k) + cenc(v) for k, v in o.items())
    raise TypeError(type(o))


def cdec(b: bytes) -> Any:
    v, i = _dec(b, 0)
    assert i == len(b), "trailing bytes"
    return v


def _dec(b: bytes, i: int):
    ib = b[i]
    i += 1
    major, ai = ib >> 5, ib & 31
    if major == 7:
        if ai in (20, 21, 22):
            return {20: False, 21: True, 22: None}[ai], i
        w = {25: ">e", 26: ">f", 27: ">d"}[ai]
        n = struct.calcsize(w)
        return struct.unpack(w, b[i:i + n])[0], i + n
    if ai < 24:
        n = ai
    else:
        size = {24: 1, 25: 2, 26: 4, 27: 8}[ai]
        n = int.from_bytes(b[i:i + size], "big")
        i += size
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major == 2:
        return bytes(b[i:i + n]), i + n
    if major == 3:
        return b[i:i + n].decode(), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = _dec(b, i)
            out.append(v)
        return out, i
    out = {}
    for _ in range(n):
        k, i = _dec(b, i)
        v, i = _dec(b, i)
        out[k] = v
    return out, i


# --------------------------------------------------------------------------- framing
def frame(ftype: int, sid: int, payload: bytes = b"", *, magic: bytes = b"SN", flags: int = 0,
          length: int | None = None) -> bytes:
    return magic + bytes([ftype, flags]) + sid.to_bytes(4, "big") + (len(payload) if length is None else length).to_bytes(4, "big") + payload


def proof_client(token: str, nonce: bytes, cnonce: bytes, spki: bytes) -> bytes:
    k = hashlib.sha256(token.encode()).digest()
    return hmac.new(k, b"SNP/1 client" + nonce + cnonce + spki, hashlib.sha256).digest()


def proof_server(token: str, nonce: bytes, cnonce: bytes, spki: bytes) -> bytes:
    k = hashlib.sha256(token.encode()).digest()
    return hmac.new(k, b"SNP/1 server" + cnonce + nonce + spki, hashlib.sha256).digest()


def tls_context(*, alpn: list[str] | None = ("snp/1",), max_version: ssl.TLSVersion | None = None,
                min_version: ssl.TLSVersion | None = ssl.TLSVersion.TLSv1_3) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the pin is checked by the caller, as the spec requires
    if min_version:
        ctx.minimum_version = min_version
    else:
        ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    if max_version:
        ctx.maximum_version = max_version
    if alpn:
        ctx.set_alpn_protocols(list(alpn))
    return ctx


class SnpError(Exception):
    def __init__(self, info: dict[str, Any]):
        super().__init__(f"{info.get('code')}: {info.get('message')}")
        self.info = info
        self.code = info.get("code")
        self.status = info.get("status")
        self.retryable = info.get("retryable")


class Raw:
    """A TLS connection with explicit frame I/O and nothing automatic."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        cert = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        spki = x509.load_der_x509_certificate(cert).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.spki = hashlib.sha256(spki).digest()
        self.pin = "sha256:" + self.spki.hex()
        self.alpn = writer.get_extra_info("ssl_object").selected_alpn_protocol()
        self.tls_version = writer.get_extra_info("ssl_object").version()

    @classmethod
    async def connect(cls, host: str, port: int, **tls) -> "Raw":
        r, w = await asyncio.open_connection(host, port, ssl=tls_context(**tls), server_hostname=None)
        return cls(r, w)

    def send(self, ftype: int, sid: int = 0, payload: bytes = b"", **kw) -> None:
        self.writer.write(frame(ftype, sid, payload, **kw))

    def send_raw(self, data: bytes) -> None:
        self.writer.write(data)

    async def flush(self) -> None:
        await self.writer.drain()

    async def recv(self, timeout: float = 5.0) -> tuple[int, int, bytes] | None:
        """Next frame, or None when the peer closed the connection."""
        try:
            hdr = await asyncio.wait_for(self.reader.readexactly(12), timeout)
            assert hdr[:2] == b"SN" and hdr[3] == 0, hdr
            length = int.from_bytes(hdr[8:12], "big")
            payload = await asyncio.wait_for(self.reader.readexactly(length), timeout) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            return None
        return hdr[2], int.from_bytes(hdr[4:8], "big"), payload

    async def expect_closed(self, timeout: float = 5.0) -> None:
        """Drains until EOF; fails if the peer keeps the connection open."""
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            assert left > 0, "connection was not closed"
            if await self.recv(left) is None:
                return

    async def hello(self, versions=(1,), features=("events", "files", "idempotency", "reasoning", "websearch"),
                    max_frame: int = 1 << 20) -> dict[str, Any]:
        self.send(HELLO, 0, cenc({"versions": list(versions), "client": "snp-test", "features": list(features),
                                  "max_frame": max_frame}))
        f = await self.recv()
        assert f is not None and f[0] == HELLO_OK, f
        return cdec(f[2])

    def close(self) -> None:
        self.writer.close()


# --------------------------------------------------------------------------- client
class CStream:
    def __init__(self, client: "Client", sid: int):
        self.client, self.id = client, sid
        self.queue: asyncio.Queue = asyncio.Queue()
        self.send_window = client.limits["initial_window"]
        self.credit_cv = asyncio.Event()
        self.done: tuple[str, Any] | None = None

    async def recv(self, timeout: float = 5.0) -> tuple[str, Any]:
        """Next event: ('msg', value) | ('data', bytes) | ('end', map) | ('error', map) | ('eof', None)."""
        kind, payload = await asyncio.wait_for(self.queue.get(), timeout)
        if kind == "msg":
            value = cdec(payload)
            await self.client.consume(self.id, len(payload))
            return "msg", value
        if kind == "data":
            await self.client.consume(self.id, len(payload))
            return "data", payload
        if kind in ("end", "error"):
            self.done = (kind, cdec(payload) if payload else {})
            return self.done
        return kind, None

    async def until_done(self, timeout: float = 10.0) -> tuple[list, list, str, Any]:
        msgs, datas = [], []
        while True:
            kind, value = await self.recv(timeout)
            if kind == "msg":
                msgs.append(value)
            elif kind == "data":
                datas.append(value)
            else:
                return msgs, datas, kind, value

    async def result(self, timeout: float = 10.0) -> Any:
        _, _, kind, value = await self.until_done(timeout)
        if kind == "error":
            raise SnpError(value)
        if kind == "eof":
            raise ConnectionError("connection closed")
        return value.get("result")

    async def send_data(self, data: bytes, timeout: float = 10.0) -> None:
        c = self.client
        n = len(data)
        end = asyncio.get_running_loop().time() + timeout
        while self.send_window < n or c.send_window < n:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError("no credit")
            c.wake.clear()
            try:
                await asyncio.wait_for(c.wake.wait(), left)
            except asyncio.TimeoutError:
                raise TimeoutError("no credit") from None
        self.send_window -= n
        c.send_window -= n
        c.raw.send(DATA, self.id, data)
        await c.raw.flush()

    def send_msg(self, value: Any) -> None:
        self.client.raw.send(MSG, self.id, cenc(value))

    def end(self, value: dict | None = None) -> None:
        self.client.raw.send(END, self.id, cenc(value or {}))

    def cancel(self) -> None:
        self.client.raw.send(CANCEL, self.id, cenc({}))


class Client:
    def __init__(self, raw: Raw, hello_ok: dict[str, Any], auth_ok: dict[str, Any]):
        self.raw = raw
        self.hello_ok, self.auth_ok = hello_ok, auth_ok
        self.limits = hello_ok["limits"]
        self.features = set(hello_ok["features"])
        self.send_window = self.limits["conn_window"]
        self.streams: dict[int, CStream] = {}
        self.next_sid = 1
        self.goaway: dict[str, Any] | None = None
        self.goaway_event = asyncio.Event()
        self.closed = asyncio.Event()
        self.auto_credit = True
        self.auto_pong = True
        self.pongs: list[bytes] = []
        self.pings: list[bytes] = []
        self.last_rx = 0.0
        self.wake = asyncio.Event()
        self.task = asyncio.create_task(self._reader())

    @classmethod
    async def connect(cls, host: str, port: int, token: str, *, pin: str | None = None, features=None,
                      max_frame: int = 1 << 20, **tls) -> "Client":
        raw = await Raw.connect(host, port, **tls)
        if pin is not None:
            assert hmac.compare_digest(raw.pin, pin), "pin mismatch"
        kw = {} if features is None else {"features": features}
        ok = await raw.hello(max_frame=max_frame, **kw)
        cnonce = os.urandom(16)
        raw.send(AUTH, 0, cenc({"method": "hmac-sha256", "cnonce": cnonce,
                                "proof": proof_client(token, ok["nonce"], cnonce, raw.spki)}))
        f = await raw.recv()
        if f is None:
            raise ConnectionError("closed during AUTH")
        if f[0] == GOAWAY:
            raise SnpError(cdec(f[2]))
        assert f[0] == AUTH_OK, f
        auth_ok = cdec(f[2])
        assert hmac.compare_digest(auth_ok["proof"], proof_server(token, ok["nonce"], cnonce, raw.spki)), \
            "server proof invalid"
        return cls(raw, ok, auth_ok)

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                f = await self.raw.recv(timeout=3600)
                if f is None:
                    break
                self.last_rx = loop.time()
                ftype, sid, payload = f
                if sid == 0:
                    if ftype == PING:
                        self.pings.append(payload)
                        if self.auto_pong:
                            self.raw.send(PONG, 0, payload)
                    elif ftype == PONG:
                        self.pongs.append(payload)
                    elif ftype == GOAWAY:
                        self.goaway = cdec(payload)
                        self.goaway_event.set()
                    elif ftype == WINDOW:
                        self.send_window += cdec(payload)["inc"]
                        self.wake.set()
                    continue
                s = self.streams.get(sid)
                if s is None:
                    continue
                if ftype == WINDOW:
                    s.send_window += cdec(payload)["inc"]
                    self.wake.set()
                elif ftype == MSG:
                    s.queue.put_nowait(("msg", payload))
                elif ftype == DATA:
                    s.queue.put_nowait(("data", payload))
                elif ftype == END:
                    s.queue.put_nowait(("end", payload))
                elif ftype == ERROR:
                    s.queue.put_nowait(("error", payload))
        finally:
            self.closed.set()
            for s in self.streams.values():
                s.queue.put_nowait(("eof", b""))

    async def consume(self, sid: int, n: int) -> None:
        if self.auto_credit and not self.closed.is_set():
            self.raw.send(WINDOW, sid, cenc({"inc": n}))
            self.raw.send(WINDOW, 0, cenc({"inc": n}))

    def grant(self, sid: int, n: int) -> None:
        """Manually give the server credit (used when auto_credit is off)."""
        self.raw.send(WINDOW, sid, cenc({"inc": n}))
        self.raw.send(WINDOW, 0, cenc({"inc": n}))

    async def open(self, op: str, args: dict | None = None, *, idem: str | None = None,
                   deadline_ms: int | None = None) -> CStream:
        sid = self.next_sid
        self.next_sid += 2
        s = CStream(self, sid)
        self.streams[sid] = s
        req: dict[str, Any] = {"op": op, "args": args or {}}
        if idem:
            req["idem"] = idem
        if deadline_ms:
            req["deadline_ms"] = deadline_ms
        self.raw.send(OPEN, sid, cenc(req))
        await self.raw.flush()
        return s

    async def call(self, op: str, args: dict | None = None, **kw) -> Any:
        s = await self.open(op, args, **kw)
        return await s.result()

    async def close(self) -> None:
        self.raw.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
