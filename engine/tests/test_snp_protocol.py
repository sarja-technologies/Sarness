"""SNP/1 framing, handshake, authentication and connection-level behaviour over real TLS sockets."""

import asyncio
import os
import ssl

import pytest

import snp_client as sc
from sarness_engine.snp import frames as fr

pytestmark = pytest.mark.asyncio

# ----------------------------------------------------------------------------- helpers
async def goaway_then_close(raw: sc.Raw, code: str | None = None) -> dict:
    """Reads until GOAWAY, checks its code and that the server then closes the connection."""
    while True:
        f = await raw.recv(5)
        assert f is not None, "connection closed without GOAWAY"
        if f[0] == sc.GOAWAY:
            body = sc.cdec(f[2])
            break
    if code:
        assert body["code"] == code, body
    await raw.expect_closed()
    return body


async def authed(env, **kw) -> sc.Client:
    c = await env.client(**kw)
    c.auto_pong = True
    return c


# ----------------------------------------------------------------------------- handshake
async def test_happy_handshake_and_limits(snp):
    c = await snp.client()
    ok = c.hello_ok
    assert ok["version"] == 1 and len(ok["nonce"]) == 32 and ok["auth"] == ["hmac-sha256"]
    assert set(ok["features"]) == {"events", "files", "idempotency", "reasoning", "websearch"}
    lim = ok["limits"]
    assert lim["max_frame"] == 1_048_576 and lim["initial_window"] == 262_144 and lim["conn_window"] == 4_194_304
    assert lim["max_streams"] == 64 and lim["ping_interval_ms"] == 15_000 and lim["idle_timeout_ms"] == 45_000
    assert lim["idem_ttl_s"] == 600
    assert c.auth_ok["api_version"] and c.auth_ok["session"]
    assert (await c.call("system.health"))["status"] == "ok"


async def test_hello_max_frame_is_honoured_and_floored(snp):
    c = await snp.client(max_frame=65536)
    assert c.limits["max_frame"] == 65536
    c2 = await snp.client(max_frame=100)
    assert c2.limits["max_frame"] == 16384


async def test_unknown_features_are_ignored(snp):
    c = await snp.client(features=["events", "telepathy"])
    assert set(c.hello_ok["features"]) == {"events"}


async def test_nonce_is_fresh_per_connection(snp):
    a, b = await snp.client(), await snp.client()
    assert a.hello_ok["nonce"] != b.hello_ok["nonce"]


async def test_pin_is_stable_and_matches_spki(snp):
    r = await snp.raw()
    assert r.pin == snp.pin and r.alpn == "snp/1" and r.tls_version == "TLSv1.3"


async def test_version_mismatch_gives_goaway_with_supported_list(snp):
    r = await snp.raw()
    r.send(sc.HELLO, 0, sc.cenc({"versions": [2, 3], "client": "t", "features": [], "max_frame": 65536}))
    body = await goaway_then_close(r, "unsupported_version")
    assert body["supported"] == [1]


async def test_highest_common_version_is_selected(snp):
    r = await snp.raw()
    ok = await r.hello(versions=(7, 1, 3))
    assert ok["version"] == 1


@pytest.mark.parametrize("versions", [[], list(range(9)), ["1"], [256], [-1], "1", None])
async def test_bad_versions_field_is_protocol_error(snp, versions):
    r = await snp.raw()
    r.send(sc.HELLO, 0, sc.cenc({"versions": versions}))
    await goaway_then_close(r, "protocol_error")


async def test_hello_missing_versions(snp):
    r = await snp.raw()
    r.send(sc.HELLO, 0, sc.cenc({"client": "x"}))
    await goaway_then_close(r, "protocol_error")


async def test_malformed_cbor_in_handshake_is_protocol_error(snp):
    r = await snp.raw()
    r.send(sc.HELLO, 0, b"\xbf\x61a\x01\xff")  # indefinite-length map
    await goaway_then_close(r, "protocol_error")


@pytest.mark.parametrize("first", [sc.AUTH, sc.PING, sc.OPEN, sc.MSG, sc.DATA, sc.WINDOW, sc.GOAWAY, sc.CANCEL])
async def test_wrong_first_frame(snp, first):
    r = await snp.raw()
    sid = 0 if first in (sc.AUTH, sc.PING, sc.GOAWAY, sc.WINDOW) else 1
    payload = bytes(8) if first == sc.PING else sc.cenc({"op": "system.health"})
    r.send(first, sid, payload)
    await goaway_then_close(r, "protocol_error")


async def test_open_before_auth_is_refused(snp):
    r = await snp.raw()
    await r.hello()
    r.send(sc.OPEN, 1, sc.cenc({"op": "settings.get"}))
    body = await goaway_then_close(r, "protocol_error")
    assert "AUTH" in body["message"]


async def test_ping_between_hello_and_auth_is_protocol_error(snp):
    r = await snp.raw()
    await r.hello()
    r.send(sc.PING, 0, bytes(8))
    await goaway_then_close(r, "protocol_error")


async def test_hello_twice(snp):
    r = await snp.raw()
    await r.hello()
    r.send(sc.HELLO, 0, sc.cenc({"versions": [1]}))
    await goaway_then_close(r, "protocol_error")


async def test_preauth_frame_limit_is_4096(snp):
    r = await snp.raw()
    r.send(sc.HELLO, 0, b"", length=4097)  # header only; the server must not wait for 4097 octets
    await r.flush()
    await goaway_then_close(r, "frame_too_large")


@pytest.mark.limits(handshake_s=0.5)
async def test_handshake_deadline_closes_silent_connection(snp):
    r = await snp.raw()
    await asyncio.wait_for(r.expect_closed(5), 3)


@pytest.mark.limits(handshake_s=0.6)
async def test_handshake_deadline_covers_missing_auth(snp):
    r = await snp.raw()
    await r.hello()
    await asyncio.wait_for(r.expect_closed(5), 3)


async def test_bad_proof(snp):
    r = await snp.raw()
    ok = await r.hello()
    r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256", "cnonce": os.urandom(16), "proof": os.urandom(32)}))
    body = await goaway_then_close(r, "unauthenticated")
    assert "proof" not in body["message"].lower()
    assert ok["nonce"]


async def test_wrong_token_fails(snp):
    with pytest.raises(sc.SnpError) as e:
        await snp.client(token="sk-not-the-token")
    assert e.value.code == "unauthenticated"


async def test_proof_bound_to_certificate(snp):
    """A proof computed for another certificate (relay to a different server) must not authenticate."""
    r = await snp.raw()
    ok = await r.hello()
    cn = os.urandom(16)
    r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256", "cnonce": cn,
                                "proof": sc.proof_client(snp.token, ok["nonce"], cn, b"\x22" * 32)}))
    await goaway_then_close(r, "unauthenticated")


async def test_unknown_method_and_bad_sizes_look_like_bad_proof(snp):
    for auth in ({"method": "plain", "cnonce": os.urandom(16), "proof": os.urandom(32)},
                 {"method": "hmac-sha256", "cnonce": os.urandom(15), "proof": os.urandom(32)},
                 {"method": "hmac-sha256", "cnonce": os.urandom(16), "proof": os.urandom(31)},
                 {"method": "hmac-sha256", "cnonce": "x" * 16, "proof": os.urandom(32)}):
        r = await snp.raw()
        await r.hello()
        r.send(sc.AUTH, 0, sc.cenc(auth))
        body = await goaway_then_close(r, "unauthenticated")
        assert body["message"] == "authentication failed"


async def test_auth_missing_field_is_protocol_error(snp):
    r = await snp.raw()
    await r.hello()
    r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256"}))
    await goaway_then_close(r, "protocol_error")


@pytest.mark.limits(auth_lockout=3, auth_fail_unit_s=0.0)
async def test_lockout_after_repeated_failures(snp):
    for _ in range(3):
        r = await snp.raw()
        await r.hello()
        r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256", "cnonce": os.urandom(16), "proof": os.urandom(32)}))
        await goaway_then_close(r, "unauthenticated")
    # Even a correct proof is now refused without being evaluated.
    with pytest.raises(sc.SnpError) as e:
        await snp.client()
    assert e.value.code == "rate_limited"


@pytest.mark.limits(auth_fail_unit_s=0.2)
async def test_failure_delay_grows_with_consecutive_failures(snp):
    delays = []
    for _ in range(3):
        r = await snp.raw()
        await r.hello()
        r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256", "cnonce": os.urandom(16), "proof": os.urandom(32)}))
        t0 = asyncio.get_running_loop().time()
        await goaway_then_close(r, "unauthenticated")
        delays.append(asyncio.get_running_loop().time() - t0)
    assert delays[0] >= 0.19 and delays[1] >= 0.39 and delays[2] >= 0.59, delays


async def test_success_resets_failure_count(snp):
    r = await snp.raw()
    await r.hello()
    r.send(sc.AUTH, 0, sc.cenc({"method": "hmac-sha256", "cnonce": os.urandom(16), "proof": os.urandom(32)}))
    await goaway_then_close(r, "unauthenticated")
    c = await snp.client()
    assert snp.server.guard._fails.get("127.0.0.1") is None
    assert (await c.call("system.health"))["status"] == "ok"


# ----------------------------------------------------------------------------- transport
async def test_plaintext_connection_is_refused(snp):
    r, w = await asyncio.open_connection(snp.host, snp.port)
    w.write(sc.frame(sc.HELLO, 0, sc.cenc({"versions": [1]})))
    await w.drain()
    data = await asyncio.wait_for(r.read(4096), 5)
    assert not data.startswith(b"SN"), "server answered SNP in plaintext"
    await asyncio.wait_for(r.read(), 5)  # then EOF
    w.close()


async def test_tls12_is_refused(snp):
    with pytest.raises((ssl.SSLError, ConnectionError)):
        await sc.Raw.connect(snp.host, snp.port, max_version=ssl.TLSVersion.TLSv1_2, min_version=None)


async def test_missing_alpn_closes_connection(snp):
    try:
        r = await sc.Raw.connect(snp.host, snp.port, alpn=None)
    except (ssl.SSLError, ConnectionError):
        return
    assert r.alpn is None
    r.send(sc.HELLO, 0, sc.cenc({"versions": [1]}))
    assert await r.recv(5) is None


async def test_wrong_alpn_closes_connection(snp):
    try:
        r = await sc.Raw.connect(snp.host, snp.port, alpn=["h2", "http/1.1"])
    except (ssl.SSLError, ConnectionError):
        return
    r.send(sc.HELLO, 0, sc.cenc({"versions": [1]}))
    assert await r.recv(5) is None


async def test_http_request_on_snp_port_gets_nothing(snp):
    r, w = await asyncio.open_connection(snp.host, snp.port)
    w.write(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
    await w.drain()
    data = await asyncio.wait_for(r.read(4096), 5)
    assert b"HTTP/1" not in data and b"200" not in data
    w.close()


# ----------------------------------------------------------------------------- post-auth framing violations
async def authed_raw(env) -> sc.Raw:
    c = await env.client()
    c.task.cancel()  # take over the socket: from here on the test speaks raw frames
    await asyncio.gather(c.task, return_exceptions=True)
    return c.raw


@pytest.mark.parametrize("kind", ["magic", "flags", "type", "oversize", "even-open", "even-msg", "not-monotonic",
                                  "reused", "never-opened", "hello-again", "server-frame", "window-overflow",
                                  "ping-len", "ping-stream"])
async def test_framing_violations_close_the_connection(snp, kind):
    r = await authed_raw(snp)
    health = sc.cenc({"op": "system.health"})
    expect = "protocol_error"
    if kind == "magic":
        r.send_raw(sc.frame(sc.PING, 0, bytes(8), magic=b"XX"))
    elif kind == "flags":
        r.send_raw(sc.frame(sc.PING, 0, bytes(8), flags=4))
    elif kind == "type":
        r.send_raw(sc.frame(0x42, 0, b""))
    elif kind == "oversize":
        r.send_raw(sc.frame(sc.MSG, 1, b"", length=1_048_577))
        expect = "frame_too_large"
    elif kind == "even-open":
        r.send(sc.OPEN, 2, health)
    elif kind == "even-msg":
        r.send(sc.MSG, 2, b"\xa0")
    elif kind == "not-monotonic":
        r.send(sc.OPEN, 5, health)
        r.send(sc.OPEN, 3, health)
    elif kind == "reused":
        r.send(sc.OPEN, 1, health)
        r.send(sc.OPEN, 1, health)
    elif kind == "never-opened":
        r.send(sc.END, 9, b"")
    elif kind == "hello-again":
        r.send(sc.HELLO, 0, sc.cenc({"versions": [1]}))
    elif kind == "server-frame":
        r.send(sc.ERROR, 1, sc.cenc({"code": "x"}))
    elif kind == "window-overflow":
        r.send(sc.WINDOW, 0, sc.cenc({"inc": 2**31 - 1}))
    elif kind == "ping-len":
        r.send(sc.PING, 0, bytes(7))
    elif kind == "ping-stream":
        r.send(sc.PING, 1, bytes(8))
    await r.flush()
    while True:
        f = await r.recv(5)
        assert f is not None, "closed without GOAWAY"
        if f[0] == sc.GOAWAY:
            assert sc.cdec(f[2])["code"] == expect
            break
    await r.expect_closed()


async def test_ping_pong_is_echoed(snp):
    r = await authed_raw(snp)
    r.send(sc.PING, 0, bytes(range(8)))
    f = await r.recv()
    assert f == (sc.PONG, 0, bytes(range(8)))
    r.send(sc.PONG, 0, bytes(8))  # unsolicited PONG is ignored
    r.send(sc.PING, 0, bytes(8))
    assert (await r.recv())[0] == sc.PONG


async def test_unknown_op_and_bad_open_are_stream_errors_only(snp):
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("no.such.op")
    assert e.value.code == "unknown_op" and e.value.status == 404
    # Malformed OPEN payloads terminate only that stream.
    for payload in (b"\xff", sc.cenc({"args": {}}), sc.cenc({"op": 5}), sc.cenc([1]), sc.cenc({"op": "settings.get", "args": 3}),
                    sc.cenc({"op": "settings.get", "idem": ""}), sc.cenc({"op": "settings.get", "idem": "x" * 65}),
                    sc.cenc({"op": "settings.get", "deadline_ms": 0}), sc.cenc({"op": "settings.get", "deadline_ms": "5"})):
        s = await c.open("settings.get")  # allocate a stream id, then replace the payload by hand
        s2 = c.next_sid
        c.next_sid += 2
        st = sc.CStream(c, s2)
        c.streams[s2] = st
        c.raw.send(sc.OPEN, s2, payload)
        kind, val = await st.recv()
        assert kind == "error" and val["code"] == "bad_request", (payload, val)
        await s.result()
    assert (await c.call("settings.get"))  # connection still healthy


async def test_missing_required_argument_and_bad_type(snp):
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("chat.conversation.get")
    assert e.value.code == "bad_request"
    with pytest.raises(sc.SnpError) as e:
        await c.call("job.list", {"limit": "many"})
    assert e.value.code == "bad_request" and e.value.status == 422
    with pytest.raises(sc.SnpError) as e:
        await c.call("generation.list", {"limit": 100000})
    assert e.value.code == "bad_request"
    # Unknown argument keys are not interpreted.
    assert await c.call("job.list", {"limit": 5, "bogus": 1}) == []
