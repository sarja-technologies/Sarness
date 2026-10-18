"""SNP/1 frame header vectors (spec 4)."""

import pytest

from sarness_engine.snp import frames as fr

PING_VECTOR = bytes.fromhex("534e0500" "00000000" "00000008" "0102030405060708")


def hdr(ftype=5, flags=0, sid=0, length=8, magic=b"SN") -> bytes:
    return magic + bytes([ftype, flags]) + sid.to_bytes(4, "big") + length.to_bytes(4, "big")


def test_spec_ping_vector_parses():
    assert fr.parse_header(PING_VECTOR[:12], 4096) == (fr.PING, 0, 8)
    assert fr.pack_frame(fr.PING, 0, bytes(range(1, 9))) == PING_VECTOR


@pytest.mark.parametrize("raw,code", [
    (hdr(magic=b"XX"), "protocol_error"),
    (hdr(magic=b"SM"), "protocol_error"),
    (hdr(flags=1), "protocol_error"),
    (hdr(flags=0x80), "protocol_error"),
    (hdr(ftype=0x00), "protocol_error"),
    (hdr(ftype=0x08), "protocol_error"),
    (hdr(ftype=0x17), "protocol_error"),
    (hdr(ftype=0xFF), "protocol_error"),
    (hdr(ftype=fr.PING, length=7), "protocol_error"),
    (hdr(ftype=fr.PING, sid=1), "protocol_error"),
    (hdr(ftype=fr.OPEN, sid=0, length=4), "protocol_error"),
    (hdr(ftype=fr.MSG, sid=0, length=4), "protocol_error"),
    (hdr(ftype=fr.MSG, sid=1, length=4097), "frame_too_large"),
    (hdr(ftype=fr.DATA, sid=1, length=2**32 - 1), "frame_too_large"),
])
def test_header_rejections(raw, code):
    with pytest.raises(fr.ProtocolError) as e:
        fr.parse_header(raw, 4096)
    assert e.value.code == code
