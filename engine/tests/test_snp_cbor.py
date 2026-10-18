"""SNP-CBOR profile (spec 4.2): rejection vectors, bounds, round trips against the independent encoder."""

import hashlib
import hmac
import math
import random
import struct

import pytest

import snp_client as sc
from sarness_engine.snp import cbor
from sarness_engine.snp.cbor import CborError


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


REJECT = {
    "indefinite byte string": "5f 41 00 ff",
    "indefinite text string": "7f 61 61 ff",
    "indefinite array": "9f 01 ff",
    "indefinite map": "bf 61 61 01 ff",
    "tag 0": "c0 61 61",
    "tag 24": "d8 18 41 00",
    "non-shortest uint (24 as 2 bytes)": "19 00 18",
    "non-shortest uint (1 as 1 byte)": "18 01",
    "non-shortest uint (0xffff as 4 bytes)": "1a 00 00 ff ff",
    "non-shortest uint (as 8 bytes)": "1b 00 00 00 00 00 00 00 ff",
    "non-shortest text length": "78 01 61",
    "non-shortest array length": "98 01 00",
    "non-shortest negative": "38 00",
    "duplicate map key": "a2 61 61 01 61 61 02",
    "non-text map key": "a1 01 02",
    "bytes map key": "a1 41 61 02",
    "huge text length, tiny payload": "7b 00 00 00 7f ff ff ff ff 61",
    "huge byte length, tiny payload": "5a ff ff ff ff 00",
    "huge array length, tiny payload": "9a 00 ff ff ff 00",
    "huge map length, tiny payload": "ba 00 ff ff ff 61 61 00",
    "64-bit length": "5b ff ff ff ff ff ff ff ff 00",
    "array needs n octets": "83 01 02",
    "map needs 2n octets": "a2 61 61 01 61",
    "trailing bytes": "01 00",
    "two items": "f6 f6",
    "invalid UTF-8": "62 c3 28",
    "lone surrogate": "63 ed a0 80",
    "overlong UTF-8": "62 c0 af",
    "NaN float16": "f9 7e 00",
    "NaN float64": "fb 7f f8 00 00 00 00 00 00",
    "Infinity float32": "fa 7f 80 00 00",
    "-Infinity float16": "f9 fc 00",
    "undefined": "f7",
    "simple value 16": "f0",
    "simple value one-byte form": "f8 20",
    "break outside indefinite": "ff",
    "reserved additional info 28": "1c",
    "uint above int64": "1b 80 00 00 00 00 00 00 00",
    "negative below int64": "3b 80 00 00 00 00 00 00 00",
    "empty payload": "",
    "truncated head": "19 01",
    "truncated float": "fb 00 00",
    "truncated string": "63 61 62",
}


@pytest.mark.parametrize("name", REJECT)
def test_rejects(name):
    with pytest.raises(CborError):
        cbor.loads(h(REJECT[name]))


def test_depth_bomb():
    ok = b"\x81" * 32 + b"\x00"
    assert cbor.loads(ok) is not None
    with pytest.raises(CborError):
        cbor.loads(b"\x81" * 33 + b"\x00")
    with pytest.raises(CborError):
        cbor.loads(b"\x81" * 100_000 + b"\x00")
    nested_maps = b"\xa1\x61a" * 33 + b"\x00"
    with pytest.raises(CborError):
        cbor.loads(nested_maps)


def test_item_limit():
    n = 100_000
    assert len(cbor.loads(b"\x9a" + struct.pack(">I", n - 1) + b"\x00" * (n - 1))) == n - 1
    with pytest.raises(CborError):
        cbor.loads(b"\x9a" + struct.pack(">I", n) + b"\x00" * n)


def test_declared_length_is_checked_before_allocation():
    # 4 GiB declared in a 6-byte payload must fail immediately and cheaply.
    with pytest.raises(CborError):
        cbor.loads(h("5a ff ff ff ff"))
    with pytest.raises(CborError):
        cbor.loads(h("9a ff ff ff ff"))


def test_int64_bounds_roundtrip():
    for v in (0, 23, 24, 255, 256, 65535, 65536, 2**32 - 1, 2**32, 2**63 - 1, -1, -24, -25, -256, -257, -(2**63)):
        assert cbor.loads(cbor.dumps(v)) == v
        assert cbor.dumps(v) == sc.cenc(v)  # shortest form equals the independent encoder
    for v in (2**63, -(2**63) - 1):
        with pytest.raises(CborError):
            cbor.dumps(v)


def test_encoder_rejects_nonfinite_and_bad_keys():
    for v in (math.nan, math.inf, -math.inf):
        with pytest.raises(CborError):
            cbor.dumps(v)
    with pytest.raises(CborError):
        cbor.dumps({1: 2})
    with pytest.raises(CborError):
        cbor.dumps(object())


def test_float_shortest_roundtrip():
    assert cbor.dumps(1.5) == h("f9 3e 00")
    assert cbor.dumps(100000.0) == h("fa 47 c3 50 00")
    assert cbor.dumps(1.1) == h("fb 3f f1 99 99 99 99 99 9a")
    assert cbor.dumps(-0.0) == h("f9 80 00")
    for x in (0.0, 1.5, 1.1, 1e300, 5e-324, 3.4028234663852886e38):
        assert cbor.loads(cbor.dumps(x)) == x


def test_map_keys_sorted_bytewise():
    enc = cbor.dumps({"b": 1, "aa": 2, "a": 3})
    assert enc == h("a3 61 61 03 61 62 01 62 61 61 02")


def test_strings_and_containers():
    v = {"s": "héllo \U0001F600", "b": b"\x00\xff", "l": [1, [2, [3]]], "n": None, "t": True, "f": False, "m": {}}
    assert cbor.loads(cbor.dumps(v)) == v
    assert cbor.loads(h("a0")) == {}
    assert cbor.loads(cbor.dumps("a\x00b")) == "a\x00b"  # NUL is valid UTF-8; the profile only requires valid UTF-8


def test_cross_implementation_roundtrip():
    rng = random.Random(7)

    def gen(depth=0):
        k = rng.randrange(9 if depth < 4 else 6)
        if k == 0:
            return rng.randrange(-(2**63), 2**63)
        if k == 1:
            return rng.choice([0, 1, 23, 24, 255, 256, 65535, 65536, 2**32, -1, -24, -25])
        if k == 2:
            return "".join(rng.choice("abcé☃\U0001F600 ") for _ in range(rng.randrange(40)))
        if k == 3:
            return bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
        if k == 4:
            return rng.choice([None, True, False])
        if k == 5:
            return rng.choice([0.0, 1.5, -2.25, 1e100, 3.14159])
        if k in (6, 7):
            return [gen(depth + 1) for _ in range(rng.randrange(5))]
        return {f"k{i}": gen(depth + 1) for i in range(rng.randrange(5))}

    for _ in range(300):
        v = gen()
        assert sc.cdec(cbor.dumps(v)) == v        # server encoder -> independent decoder
        assert cbor.loads(sc.cenc(v)) == v        # independent encoder -> server decoder


def test_spec_auth_test_vector():
    """docs/SNP.md 12.2, compared with the published constants."""
    k = hashlib.sha256(b"sk-test").digest()
    assert k.hex() == "f3abf2a6cc4f00987743db5f544ba345b4899ae31f326d8ee9c4816de153c9e0"
    proof = hmac.new(k, b"SNP/1 client" + b"\x00" * 32 + b"\x11" * 16 + b"\x22" * 32, hashlib.sha256).digest()
    assert proof.hex() == "5b2fc2282c3e222fcbeb00e7b86d643140edad01fbe84ed8c8b473418c797b3f"
    assert sc.proof_client("sk-test", b"\x00" * 32, b"\x11" * 16, b"\x22" * 32) == proof
