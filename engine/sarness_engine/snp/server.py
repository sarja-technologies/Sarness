"""SNP/1 server: TLS 1.3 listener, handshake, multiplexed streams, flow control (docs/SNP.md).

One :class:`SnpServer` accepts connections; each becomes a :class:`Connection` with a reader loop
(frame dispatch), a keep-alive task and one task per open stream. All writes happen on the
event loop thread and are single ``write`` calls, so frames never interleave.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import logging
import secrets
import socket
import ssl
import time
from typing import Any

from .. import API_VERSION, __version__
from . import cbor
from . import frames as fr
from .frames import Limits, ProtocolError
from .identity import Identity, load_or_create
from .ops import Catalogue, OpDef, OpError, call_unary, map_exception

log = logging.getLogger("sarness.snp")

SERVER_FEATURES = ("events", "files", "idempotency", "reasoning", "websearch")
SERVER_NAME = f"sarness-engine/{__version__}"


class ConnectionClosed(Exception):
    """The peer is gone (or the connection is being torn down); stream tasks end quietly."""


class _Disconnect(Exception):
    """Close without a reply (peer misbehaved after GOAWAY, or sent GOAWAY itself)."""


class _AuthFailure(Exception):
    def __init__(self, code: str, delay: float = 0.0):
        super().__init__(code)
        self.code = code
        self.delay = delay


# --------------------------------------------------------------------------- shared state
class AuthGuard:
    """Per-source-address failure accounting: back-off delay and rolling-minute lock-out (spec 5.3)."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self._fails: dict[str, collections.deque[float]] = {}

    def _recent(self, ip: str) -> collections.deque:
        q = self._fails.setdefault(ip, collections.deque())
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        return q

    def locked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.limits.auth_lockout

    def failure(self, ip: str) -> float:
        q = self._recent(ip)
        q.append(time.monotonic())
        if len(self._fails) > 4096:
            for k in [k for k, v in self._fails.items() if not v]:
                del self._fails[k]
        return self.limits.auth_fail_unit_s * min(len(q), self.limits.auth_fail_cap)

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)


class IdemRecord:
    __slots__ = ("op", "task", "expires", "noreplay")

    def __init__(self, op: str, task: asyncio.Future | None, noreplay: bool = False):
        self.op = op
        self.task = task
        self.expires: float | None = None  # set when the execution finishes
        self.noreplay = noreplay


class IdemTable:
    """Idempotency records for the single authenticated principal (spec 8): LRU, TTL, 4096 entries."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self._rec: collections.OrderedDict[str, IdemRecord] = collections.OrderedDict()

    def get(self, key: str) -> IdemRecord | None:
        rec = self._rec.get(key)
        if rec is None:
            return None
        if rec.expires is not None and rec.expires < time.monotonic():
            del self._rec[key]
            return None
        self._rec.move_to_end(key)
        return rec

    def add(self, key: str, rec: IdemRecord) -> None:
        self._rec[key] = rec
        self._rec.move_to_end(key)
        while len(self._rec) > self.limits.idem_max:
            victim = next((k for k, r in self._rec.items() if r.expires is not None or r.noreplay), None)
            self._rec.pop(victim if victim is not None else next(iter(self._rec)))

    def finish(self, key: str, rec: IdemRecord, keep: bool) -> None:
        if not keep or self.limits.idem_ttl_s <= 0:
            if self._rec.get(key) is rec:
                del self._rec[key]
            return
        rec.expires = time.monotonic() + self.limits.idem_ttl_s

    def __len__(self) -> int:
        return len(self._rec)


# --------------------------------------------------------------------------- streams
class Stream:
    """Server-side state of one client-initiated stream; also the ``ctx`` handed to native operations."""

    def __init__(self, conn: "Connection", sid: int, op: OpDef, args: dict[str, Any], idem: str | None,
                 deadline_ms: int | None):
        self.conn = conn
        self.id = sid
        self.op = op
        self.args = args
        self.idem = idem
        self.deadline_ms = deadline_ms
        self.send_window = conn.limits.initial_window
        self.recv_window = conn.limits.initial_window
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.cancel_reason = "cancelled"

    # ---- context API for operations
    @property
    def st(self) -> Any:
        return self.conn.server.st

    @property
    def chunk_size(self) -> int:
        lim = self.conn.limits
        return max(16384, min(self.conn.max_frame, lim.initial_window // 2))

    @property
    def max_msg(self) -> int:
        return min(self.conn.max_frame, self.conn.limits.initial_window)

    def _encode_msg(self, value: Any) -> bytes:
        try:
            payload = cbor.dumps(value)
        except cbor.CborError as exc:
            raise OpError("internal", f"unencodable message: {exc}") from None
        if len(payload) > self.max_msg:
            raise OpError("too_large", f"message of {len(payload)} octets exceeds the flow-control window")
        return payload

    async def send_msg(self, value: Any) -> None:
        await self._send(fr.MSG, self._encode_msg(value))

    def try_send_msg(self, value: Any) -> bool:
        """Sends only if credit is available right now; never waits. False means 'not sent'."""
        try:
            payload = self._encode_msg(value)
        except OpError:
            return False
        n = len(payload)
        if self.closed or self.send_window < n or self.conn.send_window < n:
            return False
        self.send_window -= n
        self.conn.send_window -= n
        self.conn.write(fr.pack_frame(fr.MSG, self.id, payload))
        return True

    async def send_data(self, data: bytes) -> None:
        await self._send(fr.DATA, data)

    async def _send(self, ftype: int, payload: bytes) -> None:
        n = len(payload)
        conn = self.conn
        await conn.wait_credit(self, n)
        self.send_window -= n
        conn.send_window -= n
        conn.write(fr.pack_frame(ftype, self.id, payload))
        await conn.drain()

    def consumed(self, nbytes: int) -> None:
        """Returns flow-control credit after inbound data has been processed."""
        if self.closed or nbytes <= 0:
            return
        self.recv_window += nbytes
        self.conn.send_window_update(self.id, nbytes)
        self.conn.credit_connection(nbytes)


# --------------------------------------------------------------------------- connection
class Connection:
    def __init__(self, server: "SnpServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ip: str):
        self.server = server
        self.limits = server.limits
        self.reader = reader
        self.writer = writer
        self.ip = ip
        self.loop = asyncio.get_running_loop()
        self.authed = False
        self.max_frame = fr.PRE_AUTH_MAX_FRAME
        self.features: set[str] = set()
        self.streams: dict[int, Stream] = {}
        self.last_sid = 0
        self.send_window = self.limits.conn_window
        self.recv_window = self.limits.conn_window
        self.last_rx = self.loop.time()
        self.goaway_sent = False
        self.closed = False
        self._waiters: list[asyncio.Future] = []
        self._keepalive: asyncio.Task | None = None

    # ---- low-level output
    def write(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.writer.write(data)
        except Exception:  # noqa: BLE001 - transport already gone
            self.abort()
            return
        tr = self.writer.transport
        if tr is not None and tr.get_write_buffer_size() > self.limits.conn_window + 4 * self.limits.max_frame + (1 << 20):
            log.warning("SNP peer %s is not reading; dropping connection", self.ip)
            self.abort()

    async def drain(self) -> None:
        if self.closed:
            raise ConnectionClosed
        try:
            await self.writer.drain()
        except (ConnectionError, OSError):
            raise ConnectionClosed from None

    def send_map(self, ftype: int, sid: int, value: dict[str, Any]) -> None:
        self.write(fr.pack_frame(ftype, sid, cbor.dumps(value)))

    def send_window_update(self, sid: int, inc: int) -> None:
        self.send_map(fr.WINDOW, sid, {"inc": inc})

    def credit_connection(self, inc: int) -> None:
        if inc > 0:
            self.recv_window += inc
            self.send_window_update(0, inc)

    def abort(self) -> None:
        self.closed = True
        self._wake()
        tr = self.writer.transport
        if tr is not None:
            tr.abort()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._wake()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.writer.drain(), 1.0)
            with contextlib.suppress(Exception):
                self.writer.close()

    # ---- credit waiting
    def _wake(self) -> None:
        for f in self._waiters:
            if not f.done():
                f.set_result(None)
        self._waiters.clear()

    async def wait_credit(self, s: Stream, n: int) -> None:
        """Blocks until both the stream and the connection can take ``n`` octets; stall => ERROR stalled."""
        deadline = self.loop.time() + self.limits.stall_s
        while True:
            if self.closed:
                raise ConnectionClosed
            if s.closed:
                raise ConnectionClosed
            if s.send_window >= n and self.send_window >= n:
                return
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                raise OpError("stalled", "no flow-control credit from the client")
            fut = self.loop.create_future()
            self._waiters.append(fut)
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                raise OpError("stalled", "no flow-control credit from the client") from None
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)

    # ---- input
    async def read_frame(self, max_payload: int) -> tuple[int, int, bytes]:
        header = await self.reader.readexactly(fr.HEADER_LEN)
        ftype, sid, length = fr.parse_header(header, max_payload)
        payload = await self.reader.readexactly(length) if length else b""
        self.last_rx = self.loop.time()
        return ftype, sid, payload

    @staticmethod
    def _map(payload: bytes, what: str) -> dict[str, Any]:
        try:
            value = cbor.loads(payload) if payload else {}
        except cbor.CborError as exc:
            raise ProtocolError(f"malformed {what}: {exc}") from None
        if not isinstance(value, dict):
            raise ProtocolError(f"{what} must be a map")
        return value

    # ---- lifecycle
    async def run(self, accepted_at: float) -> None:
        try:
            try:
                async with asyncio.timeout_at(accepted_at + self.limits.handshake_s):
                    await self._handshake()
            except TimeoutError:
                log.info("SNP handshake from %s timed out", self.ip)
                return
            self._keepalive = asyncio.create_task(self._keepalive_loop())
            await self._main()
        except ProtocolError as exc:
            log.info("SNP protocol error from %s: %s", self.ip, exc)
            await self.goaway(exc.code, str(exc), **exc.extra)
        except _AuthFailure as exc:
            if exc.delay:
                await asyncio.sleep(exc.delay)
            await self.goaway(exc.code, "authentication failed" if exc.code == "unauthenticated" else "too many attempts")
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ConnectionClosed, _Disconnect):
            pass
        except Exception:  # noqa: BLE001
            log.exception("SNP connection from %s crashed", self.ip)
        finally:
            await self._teardown()

    async def goaway(self, code: str, message: str, **extra: Any) -> None:
        if self.goaway_sent or self.closed:
            await self.close()
            return
        self.goaway_sent = True
        body = {"code": code, "message": message[:256], **extra}
        if self.authed:
            body.setdefault("last_stream_id", self.last_sid)
        self.send_map(fr.GOAWAY, 0, body)
        await self.close()

    async def _teardown(self) -> None:
        self.closed = True
        self._wake()
        if self._keepalive:
            self._keepalive.cancel()
        tasks = [s.task for s in self.streams.values() if s.task]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._keepalive:
            await asyncio.gather(self._keepalive, return_exceptions=True)
        with contextlib.suppress(Exception):
            self.writer.close()

    async def shutdown(self) -> None:
        """Graceful stop: GOAWAY(shutdown), let running streams finish for ``drain_s``, then close."""
        if self.closed:
            return
        if not self.authed:
            self.abort()
            return
        self.goaway_sent = True
        self.send_map(fr.GOAWAY, 0, {"code": "shutdown", "message": "server is shutting down",
                                     "last_stream_id": self.last_sid})
        for s in list(self.streams.values()):
            if s.op.mode == "subscribe" and s.task:
                s.cancel_reason = "shutdown"
                s.task.cancel()
        deadline = self.loop.time() + self.limits.drain_s
        while self.streams and self.loop.time() < deadline and not self.closed:
            await asyncio.sleep(0.02)
        await self.close()

    # ---- handshake (spec 5)
    async def _handshake(self) -> None:
        ftype, _, payload = await self.read_frame(fr.PRE_AUTH_MAX_FRAME)
        if ftype != fr.HELLO:
            raise ProtocolError("expected HELLO")
        hello = self._map(payload, "HELLO")
        versions = hello.get("versions")
        if not isinstance(versions, list) or not versions or len(versions) > 8 or \
                not all(isinstance(v, int) and 0 <= v <= 255 for v in versions):
            raise ProtocolError("HELLO.versions must be a list of 1..8 small integers")
        client_max = hello.get("max_frame", self.limits.max_frame)
        if not isinstance(client_max, int) or not 0 <= client_max <= 0xFFFFFFFF:
            raise ProtocolError("HELLO.max_frame must be u32")
        feats = hello.get("features", [])
        if not isinstance(feats, list) or len(feats) > 32 or not all(isinstance(f, str) for f in feats):
            raise ProtocolError("HELLO.features must be a list of at most 32 texts")
        client = hello.get("client", "")
        if not isinstance(client, str) or len(client) > 64:
            raise ProtocolError("HELLO.client must be text of at most 64 characters")
        common = sorted(set(versions) & set(fr.PROTOCOL_VERSIONS))
        if not common:
            raise ProtocolError("no common protocol version", "unsupported_version", supported=list(fr.PROTOCOL_VERSIONS))
        self.features = set(feats) & set(SERVER_FEATURES)
        max_frame = max(16384, min(self.limits.max_frame, client_max))
        nonce = secrets.token_bytes(32)
        self.send_map(fr.HELLO_OK, 0, {
            "version": common[-1], "server": SERVER_NAME, "nonce": nonce,
            "features": [f for f in SERVER_FEATURES if f in self.features], "auth": ["hmac-sha256"],
            "limits": self.limits.wire(max_frame)})
        await self.drain()

        ftype, _, payload = await self.read_frame(fr.PRE_AUTH_MAX_FRAME)
        if ftype != fr.AUTH:
            raise ProtocolError("expected AUTH")
        auth = self._map(payload, "AUTH")
        if not all(k in auth for k in ("method", "cnonce", "proof")):
            raise ProtocolError("AUTH is missing a field")
        if self.server.guard.locked(self.ip):
            raise _AuthFailure("rate_limited")
        method, cnonce, proof = auth["method"], auth["cnonce"], auth["proof"]
        spki = self.server.identity.spki_sha256
        key = self.server.token_key()
        ok = (method == "hmac-sha256" and isinstance(cnonce, bytes) and len(cnonce) == 16
              and isinstance(proof, bytes) and len(proof) == 32 and bool(key))
        if ok:
            expected = hmac.new(key, b"SNP/1 client" + nonce + cnonce + spki, hashlib.sha256).digest()
            ok = hmac.compare_digest(proof, expected)
        if not ok:
            log.warning("SNP authentication failed from %s", self.ip)
            raise _AuthFailure("unauthenticated", self.server.guard.failure(self.ip))
        self.server.guard.success(self.ip)
        server_proof = hmac.new(key, b"SNP/1 server" + cnonce + nonce + spki, hashlib.sha256).digest()
        self.max_frame = max_frame
        self.authed = True
        self.send_map(fr.AUTH_OK, 0, {"proof": server_proof, "session": secrets.token_hex(8),
                                      "api_version": API_VERSION})
        await self.drain()

    # ---- keep-alive (spec 9)
    async def _keepalive_loop(self) -> None:
        ping, idle = self.limits.ping_interval_ms / 1000, self.limits.idle_timeout_ms / 1000
        tick = max(0.02, min(ping, idle) / 4)
        last_ping = 0.0
        while not self.closed:
            await asyncio.sleep(tick)
            now = self.loop.time()
            quiet = now - self.last_rx
            if quiet >= idle:
                log.info("SNP connection from %s idle for %.0fs", self.ip, quiet)
                await self.goaway("idle_timeout", "no frames received")
                return
            if quiet >= ping and now - last_ping >= ping:
                last_ping = now
                self.write(fr.pack_frame(fr.PING, 0, secrets.token_bytes(8)))

    # ---- main loop
    async def _main(self) -> None:
        while True:
            ftype, sid, payload = await self.read_frame(self.max_frame)
            if ftype not in fr.CLIENT_TO_SERVER:
                raise ProtocolError(f"{fr.TYPE_NAMES[ftype]} is not valid from a client")
            if ftype in (fr.HELLO, fr.AUTH):
                raise ProtocolError(f"{fr.TYPE_NAMES[ftype]} after authentication")
            if ftype == fr.PING:
                self.write(fr.pack_frame(fr.PONG, 0, payload))
            elif ftype == fr.PONG:
                pass
            elif ftype == fr.GOAWAY:
                raise _Disconnect
            elif ftype == fr.OPEN:
                self._on_open(sid, payload)
            elif ftype == fr.WINDOW:
                self._on_window(sid, payload)
            else:
                self._on_stream_frame(ftype, sid, payload)

    def _reject(self, sid: int, err: OpError) -> None:
        self.send_map(fr.ERROR, sid, err.wire())

    def _on_open(self, sid: int, payload: bytes) -> None:
        if sid % 2 == 0:
            raise ProtocolError("client stream ids must be odd")
        if sid <= self.last_sid:
            raise ProtocolError("stream ids must increase strictly")
        self.last_sid = sid
        if self.goaway_sent:
            raise _Disconnect
        if len(payload) > self.limits.args_max:
            return self._reject(sid, OpError("too_large", "OPEN exceeds the argument size limit"))
        try:
            req = cbor.loads(payload)
        except cbor.CborError as exc:
            return self._reject(sid, OpError("bad_request", f"malformed OPEN: {exc}"))
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return self._reject(sid, OpError("bad_request", "OPEN needs a text 'op'"))
        args = req.get("args")
        args = {} if args is None else args
        idem, deadline = req.get("idem"), req.get("deadline_ms")
        if not isinstance(args, dict):
            return self._reject(sid, OpError("bad_request", "'args' must be a map"))
        if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 64):
            return self._reject(sid, OpError("bad_request", "'idem' must be 1..64 characters"))
        if deadline is not None and (not isinstance(deadline, int) or isinstance(deadline, bool)
                                     or not 0 < deadline <= 0xFFFFFFFF):
            return self._reject(sid, OpError("bad_request", "'deadline_ms' must be a positive u32"))
        op = self.server.catalogue.get(req["op"])
        if op is None:
            return self._reject(sid, OpError("unknown_op", "unknown operation"))
        if len(self.streams) >= self.limits.max_streams:
            return self._reject(sid, OpError("resource_exhausted", "too many concurrent streams"))
        s = Stream(self, sid, op, args, idem, deadline)
        self.streams[sid] = s
        s.task = asyncio.create_task(self._run_stream(s))

    def _lookup(self, sid: int) -> Stream | None:
        if sid % 2 == 0:
            raise ProtocolError("stream id of a client frame must be odd")
        if sid > self.last_sid:
            raise ProtocolError("frame for a stream that was never opened")
        return self.streams.get(sid)

    def _on_window(self, sid: int, payload: bytes) -> None:
        try:
            body = cbor.loads(payload) if payload else {}
        except cbor.CborError as exc:
            raise ProtocolError(f"malformed WINDOW: {exc}") from None
        inc = body.get("inc") if isinstance(body, dict) else None
        if not isinstance(inc, int) or isinstance(inc, bool) or not 0 <= inc <= 0xFFFFFFFF:
            raise ProtocolError("WINDOW.inc must be a u32")
        if sid == 0:
            self.send_window += inc
            if self.send_window > fr.MAX_WINDOW:
                raise ProtocolError("connection window exceeds 2^31-1")
        else:
            s = self._lookup(sid)
            if s is None:
                return
            s.send_window += inc
            if s.send_window > fr.MAX_WINDOW:
                raise ProtocolError("stream window exceeds 2^31-1")
        self._wake()

    def _on_stream_frame(self, ftype: int, sid: int, payload: bytes) -> None:
        s = self._lookup(sid)
        n = len(payload)
        if ftype in (fr.MSG, fr.DATA):
            self.recv_window -= n
            if self.recv_window < 0:
                raise ProtocolError("connection flow-control window exceeded")
            if s is not None:
                s.recv_window -= n
                if s.recv_window < 0:
                    raise ProtocolError("stream flow-control window exceeded")
            else:
                self.credit_connection(n)  # late frame for a closed stream: discard, give the credit back
                return
        if s is None:
            return
        if ftype == fr.CANCEL:
            if s.task:
                s.task.cancel()
        elif ftype == fr.END:
            try:
                value = cbor.loads(payload) if payload else {}
            except cbor.CborError as exc:
                return self._kill(s, OpError("bad_request", f"malformed END: {exc}"))
            s.inbox.put_nowait(("end", value, 0))
        elif ftype == fr.DATA:
            if s.op.mode != "upload":
                self.credit_connection(n)
                return self._kill(s, OpError("bad_request", "this operation does not accept DATA"))
            s.inbox.put_nowait(("data", payload, n))
        elif ftype == fr.MSG:
            if s.op.mode != "subscribe":
                self.credit_connection(n)
                return self._kill(s, OpError("bad_request", "this operation does not accept MSG"))
            try:
                value = cbor.loads(payload)
            except cbor.CborError as exc:
                self.credit_connection(n)
                return self._kill(s, OpError("bad_request", f"malformed MSG: {exc}"))
            s.inbox.put_nowait(("msg", value, n))

    def _kill(self, s: Stream, err: OpError) -> None:
        """Terminates one stream with a stream-level error; the connection is unaffected."""
        if s.closed:
            return
        self._finish(s, fr.ERROR, err.wire())
        if s.task:
            s.task.cancel()

    # ---- stream execution
    def _finish(self, s: Stream, ftype: int, body: dict[str, Any]) -> None:
        """Sends the terminal frame and closes the stream (idempotent). END and ERROR are bounded by
        ``max_frame`` like every frame: a result that does not fit becomes ERROR too_large."""
        if s.closed:
            return
        s.closed = True
        try:
            payload = cbor.dumps(body)
        except cbor.CborError as exc:
            log.error("unencodable result of %s: %s", s.op.name, exc)
            ftype, payload = fr.ERROR, cbor.dumps(OpError("internal", "unencodable result").wire())
        if len(payload) > self.max_frame:
            if ftype == fr.ERROR:
                body = {k: v for k, v in body.items() if k != "detail"}
                payload = cbor.dumps(body)
            else:
                err = OpError("too_large", f"result of {len(payload)} octets exceeds max_frame {self.max_frame}")
                ftype, payload = fr.ERROR, cbor.dumps(err.wire())
        self.write(fr.pack_frame(ftype, s.id, payload))
        self.streams.pop(s.id, None)
        pending = 0
        while not s.inbox.empty():
            pending += s.inbox.get_nowait()[2]
        self.credit_connection(pending)
        self._wake()

    async def _run_stream(self, s: Stream) -> None:
        try:
            result = await self._execute(s)
            self._finish(s, fr.END, {} if result is None else {"result": result})
        except asyncio.CancelledError:
            if not self.closed:
                if s.cancel_reason == "shutdown":
                    self._finish(s, fr.END, {})
                else:
                    self._finish(s, fr.ERROR, OpError("cancelled", "cancelled").wire())
        except ConnectionClosed:
            pass
        except OpError as exc:
            self._finish(s, fr.ERROR, exc.wire())
        except Exception as exc:  # noqa: BLE001
            self._finish(s, fr.ERROR, map_exception(exc).wire())
        finally:
            s.closed = True
            self.streams.pop(s.id, None)

    async def _execute(self, s: Stream) -> Any:
        op = s.op
        if op.error:
            raise OpError("internal", f"operation {op.name} is not available on this server")
        deadline = s.deadline_ms / 1000 if s.deadline_ms else None
        if deadline is None and op.mode == "unary" and not op.long_running:
            deadline = self.limits.default_unary_deadline_s
        cm = asyncio.timeout(deadline)
        try:
            async with cm:
                if op.name == "chat.generate":
                    self._claim_generate_key(s)
                if op.native is not None:
                    return await op.native(s, s.args)
                return await self._unary(s)
        except TimeoutError:
            if cm.expired():
                raise OpError("deadline_exceeded", "deadline exceeded") from None
            raise

    # ---- idempotency (spec 8)
    def _idem_key(self, s: Stream) -> str | None:
        return s.idem if s.idem and "idempotency" in self.features else None

    def _claim_generate_key(self, s: Stream) -> None:
        key = self._idem_key(s)
        if not key:
            return
        table = self.server.idem
        if table.get(key) is not None:
            raise OpError("conflict", "idempotency key already used; reconcile with chat.conversation.get")
        rec = IdemRecord(s.op.name, None, noreplay=True)
        table.add(key, rec)
        table.finish(key, rec, keep=True)

    async def _unary(self, s: Stream) -> Any:
        op, st = s.op, self.server.st
        key = self._idem_key(s)
        if key is None or op.method == "GET":
            return await call_unary(op, s.args, st)
        table = self.server.idem
        rec = table.get(key)
        if rec is not None:
            if rec.op != op.name or rec.task is None:
                raise OpError("conflict", "idempotency key was used for a different operation")
            task = rec.task
        else:
            task = asyncio.ensure_future(self._outcome(op, s.args))
            rec = IdemRecord(op.name, task)
            table.add(key, rec)
            self.server.detached.add(task)

            def _done(t: asyncio.Future, key=key, rec=rec) -> None:
                self.server.detached.discard(t)
                if t.cancelled():
                    return table.finish(key, rec, False)
                kind, val = t.result()
                table.finish(key, rec, kind == "ok" or not (val.retryable or val.code == "cancelled"))

            task.add_done_callback(_done)
        # Shielded: a cancelled, timed-out or disconnected stream must not abort an execution whose
        # outcome a retry with the same key is entitled to receive.
        kind, value = await asyncio.shield(task)
        if kind == "err":
            raise value
        return value

    async def _outcome(self, op: OpDef, args: dict[str, Any]) -> tuple[str, Any]:
        try:
            return "ok", await call_unary(op, args, self.server.st)
        except OpError as exc:
            return "err", exc
        except asyncio.CancelledError:
            return "err", OpError("cancelled", "server stopped before the operation finished")
        except Exception as exc:  # noqa: BLE001
            return "err", map_exception(exc)


# --------------------------------------------------------------------------- server
class SnpServer:
    """Listener plus the state shared by all connections (identity, auth guard, idempotency table)."""

    def __init__(self, st: Any, *, limits: Limits | None = None, identity: Identity | None = None,
                 host: str | None = None, port: int | None = None, catalogue: Catalogue | None = None):
        cfg = st.cfg
        self.st = st
        self.limits = limits or Limits()
        self.host = host or cfg.snp_host or cfg.host
        self.port = cfg.snp_port if port is None else port
        self.identity = identity or load_or_create(cfg.root, self.host, [cfg.host, *(cfg.snp_names or [])])
        self.catalogue = catalogue or Catalogue()
        self.guard = AuthGuard(self.limits)
        self.idem = IdemTable(self.limits)
        self.detached: set[asyncio.Future] = set()
        self.conns: set[Connection] = set()
        self._per_ip: collections.Counter[str] = collections.Counter()
        self._total = 0
        self._ctx = self.identity.ssl_context()
        self._lsock: socket.socket | None = None
        self._accept_task: asyncio.Task | None = None
        self._serve_tasks: set[asyncio.Task] = set()
        self._stopping = False

    @property
    def pin(self) -> str:
        return self.identity.pin

    def token_key(self) -> bytes:
        """K = SHA-256(api token), exactly the digest the engine stores as ``token_sha256``."""
        try:
            return bytes.fromhex(self.st.cfg.token_sha256)
        except ValueError:
            return b""

    async def start(self) -> int:
        family, _, _, _, addr = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM,
                                                   flags=socket.AI_PASSIVE)[0]
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(addr)
            sock.listen(128)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._lsock = sock
        self.port = sock.getsockname()[1]
        self._accept_task = asyncio.create_task(self._accept_loop())
        log.info("SNP/1 listening on %s:%d (pin %s)", self.host, self.port, self.pin)
        return self.port

    async def stop(self) -> None:
        self._stopping = True
        if self._accept_task:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
        if self._lsock:
            self._lsock.close()
        await asyncio.gather(*(c.shutdown() for c in list(self.conns)), return_exceptions=True)
        for t in list(self._serve_tasks):
            t.cancel()
        await asyncio.gather(*self._serve_tasks, return_exceptions=True)
        for t in list(self.detached):
            t.cancel()
        await asyncio.gather(*self.detached, return_exceptions=True)

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._lsock is not None
        while True:
            try:
                sock, addr = await loop.sock_accept(self._lsock)
            except asyncio.CancelledError:
                raise
            except OSError:
                if self._stopping:
                    return
                log.exception("SNP accept failed")
                await asyncio.sleep(0.1)
                continue
            ip = addr[0]
            if self._total >= self.limits.max_connections or self._per_ip[ip] >= self.limits.max_connections_per_ip:
                sock.close()  # refused before TLS: costs the server nothing
                continue
            sock.setblocking(False)
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._total += 1
            self._per_ip[ip] += 1
            t = asyncio.create_task(self._serve(sock, ip, loop.time()))
            self._serve_tasks.add(t)
            t.add_done_callback(self._serve_tasks.discard)

    async def _serve(self, sock: socket.socket, ip: str, accepted_at: float) -> None:
        loop = asyncio.get_running_loop()
        conn: Connection | None = None
        owned = False  # once handed to the event loop's transport, only the transport may close the socket
        try:
            reader = asyncio.StreamReader(limit=1 << 18)
            proto = asyncio.StreamReaderProtocol(reader)
            owned = True
            transport, _ = await loop.connect_accepted_socket(
                lambda: proto, sock, ssl=self._ctx, ssl_handshake_timeout=self.limits.handshake_s)
            writer = asyncio.StreamWriter(transport, proto, reader, loop)
            ssl_obj = transport.get_extra_info("ssl_object")
            if ssl_obj is None or ssl_obj.selected_alpn_protocol() != fr.ALPN:
                log.info("SNP: %s did not negotiate ALPN %s", ip, fr.ALPN)
                transport.abort()
                return
            conn = Connection(self, reader, writer, ip)
            self.conns.add(conn)
            await conn.run(accepted_at)
        except (ssl.SSLError, OSError, asyncio.TimeoutError) as exc:
            log.debug("SNP connection from %s failed before the protocol started: %s", ip, exc)
        finally:
            if conn is not None:
                self.conns.discard(conn)
            self._total -= 1
            self._per_ip[ip] -= 1
            if self._per_ip[ip] <= 0:
                del self._per_ip[ip]
            if not owned:
                with contextlib.suppress(Exception):
                    sock.close()
