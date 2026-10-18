"""SNP/1 frame header, frame types and protocol limits (docs/SNP.md sections 4, 5.2 and 11)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"SN"
HEADER = struct.Struct(">2sBBII")
HEADER_LEN = HEADER.size  # 12
PRE_AUTH_MAX_FRAME = 4096
MAX_WINDOW = 2**31 - 1
PROTOCOL_VERSIONS = (1,)
ALPN = "snp/1"

# Frame types.
HELLO, HELLO_OK, AUTH, AUTH_OK, PING, PONG, GOAWAY = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
OPEN, MSG, DATA, END, ERROR, CANCEL, WINDOW = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16

TYPE_NAMES = {HELLO: "HELLO", HELLO_OK: "HELLO_OK", AUTH: "AUTH", AUTH_OK: "AUTH_OK", PING: "PING", PONG: "PONG",
              GOAWAY: "GOAWAY", OPEN: "OPEN", MSG: "MSG", DATA: "DATA", END: "END", ERROR: "ERROR",
              CANCEL: "CANCEL", WINDOW: "WINDOW"}
CONNECTION_TYPES = {HELLO, HELLO_OK, AUTH, AUTH_OK, PING, PONG, GOAWAY}  # stream id MUST be 0
STREAM_TYPES = {OPEN, MSG, DATA, END, CANCEL}  # stream id MUST be non-zero
CLIENT_TO_SERVER = {HELLO, AUTH, PING, PONG, GOAWAY, OPEN, MSG, DATA, END, CANCEL, WINDOW}


class ProtocolError(Exception):
    """A connection-level violation; ``code`` is the GOAWAY code to send before closing."""

    def __init__(self, message: str, code: str = "protocol_error", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra  # additional GOAWAY fields, e.g. supported=[1]


def parse_header(raw: bytes, max_payload: int) -> tuple[int, int, int]:
    """Validates a 12-octet header and returns ``(type, stream_id, length)``.

    The payload length is checked here, before the caller reads or allocates anything for it.
    """
    magic, ftype, flags, stream, length = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if flags != 0:
        raise ProtocolError("flags must be zero")
    if ftype not in TYPE_NAMES:
        raise ProtocolError(f"unknown frame type 0x{ftype:02x}")
    if length > max_payload:
        raise ProtocolError(f"frame of {length} octets exceeds the limit {max_payload}", "frame_too_large")
    if ftype in CONNECTION_TYPES and stream != 0:
        raise ProtocolError(f"{TYPE_NAMES[ftype]} must use stream 0")
    if ftype in STREAM_TYPES and stream == 0:
        raise ProtocolError(f"{TYPE_NAMES[ftype]} needs a stream id")
    if ftype in (PING, PONG) and length != 8:
        raise ProtocolError("PING/PONG carry exactly 8 octets")
    return ftype, stream, length


def pack_frame(ftype: int, stream: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(MAGIC, ftype, 0, stream, len(payload)) + payload


@dataclass
class Limits:
    """The values advertised in HELLO_OK.limits plus server-side knobs that are not on the wire.

    Every limit is configurable (spec section 11); the defaults are the RECOMMENDED ones."""

    max_frame: int = 1_048_576
    initial_window: int = 262_144
    conn_window: int = 4_194_304
    max_streams: int = 64
    ping_interval_ms: int = 15_000
    idle_timeout_ms: int = 45_000
    idem_ttl_s: int = 600
    # Server-side only.
    max_connections: int = 32
    max_connections_per_ip: int = 8
    handshake_s: float = 10.0
    stall_s: float = 30.0
    drain_s: float = 5.0
    args_max: int = 4 * 1024 * 1024
    idem_max: int = 4096
    sub_queue: int = 512
    auth_fail_unit_s: float = 0.25
    auth_fail_cap: int = 8
    auth_lockout: int = 20
    default_unary_deadline_s: float = 60.0

    def wire(self, max_frame: int) -> dict[str, Any]:
        return {"max_frame": max_frame, "initial_window": self.initial_window, "conn_window": self.conn_window,
                "max_streams": self.max_streams, "ping_interval_ms": self.ping_interval_ms,
                "idle_timeout_ms": self.idle_timeout_ms, "idem_ttl_s": self.idem_ttl_s}
