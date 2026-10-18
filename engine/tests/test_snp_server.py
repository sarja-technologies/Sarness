"""SNP server behaviour: limits, idempotency, deadlines, liveness, shutdown, concurrency."""

import asyncio
import ssl

import pytest
from fastapi import HTTPException

import snp_client as sc
from sarness_engine.api.deps import AppState
from sarness_engine.snp import ops as ops_mod

pytestmark = pytest.mark.asyncio


async def wait_until(pred, timeout=5.0, step=0.02):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not reached in time")


def patch_op(snp, name, fn):
    if not asyncio.iscoroutinefunction(fn) and not hasattr(fn, "__code__"):
        probe = fn

        async def fn(st: AppState):  # noqa: F811 - a plain coroutine function the dispatcher recognises
            return await probe(st)

    op = snp.server.catalogue.get(name)
    op.fn, op.spec = fn, ops_mod._param_specs(fn)


class Probe:
    """A replacement route function that counts calls and can be slow or fail."""

    def __init__(self, delay=0.0, fail=None):
        self.calls = 0
        self.cancelled = 0
        self.delay = delay
        self.fail = fail or []  # exceptions raised on the first calls, in order

    async def __call__(self, st: AppState):
        n = self.calls
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if n < len(self.fail):
            raise self.fail[n]
        return {"n": n}


# ================================================================== stream and connection limits
@pytest.mark.limits(max_streams=4)
async def test_max_streams_exhaustion(snp):
    patch_op(snp, "tag.list", Probe(delay=30))
    c = await snp.client()
    assert c.limits["max_streams"] == 4
    held = [await c.open("tag.list") for _ in range(4)]
    await asyncio.sleep(0.1)
    extra = await c.open("tag.list")
    kind, v = await extra.recv()
    assert kind == "error" and v["code"] == "resource_exhausted" and v["status"] == 503 and v["retryable"] is True
    held[0].cancel()
    assert (await held[0].until_done())[3]["code"] == "cancelled"
    again = await c.open("tag.list")
    await asyncio.sleep(0.1)
    assert again.queue.empty()  # accepted this time
    for s in held[1:] + [again]:
        s.cancel()
        await s.until_done()
    assert (await c.call("settings.get"))
    await wait_until(lambda: not any(x.streams for x in snp.server.conns))


@pytest.mark.limits(max_connections_per_ip=3)
async def test_per_ip_connection_limit(snp):
    raws = [await snp.raw() for _ in range(3)]
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await sc.Raw.connect(snp.host, snp.port)  # closed by the server before TLS completes
    raws[0].close()
    await wait_until(lambda: snp.server._total == 2)
    r = await snp.raw()
    ok = await r.hello()
    assert ok["version"] == 1
    for x in raws[1:] + [r]:
        x.close()
    await wait_until(lambda: snp.server._total == 0 and not snp.server._per_ip)


@pytest.mark.limits(max_connections=2, max_connections_per_ip=8)
async def test_total_connection_limit(snp):
    a, b = await snp.raw(), await snp.raw()
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await sc.Raw.connect(snp.host, snp.port)
    a.close()
    b.close()
    await wait_until(lambda: snp.server._total == 0)
    c = await snp.client()
    assert (await c.call("settings.get"))


@pytest.mark.limits(args_max=1000)
async def test_open_larger_than_args_limit(snp):
    c = await snp.client()
    s = await c.open("chat.conversation.create", {"body": {"title": "x" * 1500}})
    _, _, kind, val = await s.until_done()
    assert val["code"] == "too_large" and val["status"] == 413
    assert (await c.call("settings.get"))


async def test_result_larger_than_max_frame_is_an_error_not_a_bad_frame(snp):
    c2 = await snp.client()
    conv = await c2.call("chat.conversation.create", {"body": {"system_prompt": "s" * 40000}})
    c = await snp.client(max_frame=16384)
    with pytest.raises(sc.SnpError) as e:
        await c.call("chat.conversation.get", {"cid": conv["id"]})
    assert e.value.code == "too_large" and e.value.status == 413
    assert len((await c2.call("chat.conversation.get", {"cid": conv["id"]}))["system_prompt"]) == 40000


async def test_late_frames_for_closed_streams_are_discarded_and_credit_returned(snp):
    c = await snp.client()
    s = await c.open("settings.get")
    await s.result()
    conn = next(iter(snp.server.conns))
    for _ in range(5):
        c.raw.send(sc.MSG, s.id, sc.cenc({"x": 1}))
        c.raw.send(sc.DATA, s.id, b"d" * 1000)
        c.raw.send(sc.WINDOW, s.id, sc.cenc({"inc": 10}))
        c.raw.send(sc.END, s.id, b"")
        c.raw.send(sc.CANCEL, s.id, b"")
    assert (await c.call("job.list")) == []
    await asyncio.sleep(0.1)
    assert conn.recv_window == c.limits["conn_window"]
    c.raw.send(sc.WINDOW, s.id, sc.cenc({"inc": 2**31 - 1}))  # also ignored on a closed stream
    assert (await c.call("job.list")) == []


async def test_stream_window_overflow_is_a_protocol_error(snp):
    patch_op(snp, "tag.list", Probe(delay=5))
    c = await snp.client()
    s = await c.open("tag.list")
    await asyncio.sleep(0.1)
    c.raw.send(sc.WINDOW, s.id, sc.cenc({"inc": 2**31 - 1}))
    await asyncio.wait_for(c.goaway_event.wait(), 5)
    assert c.goaway["code"] == "protocol_error"


# ================================================================== concurrency
@pytest.mark.limits(max_connections=64, max_connections_per_ip=32)
async def test_sixteen_clients_by_eight_streams(snp):
    clients = [await snp.client() for _ in range(16)]

    async def worker(i, c):
        names = [f"p{i}-{j}" for j in range(8)]
        txt = f"client {i} says hello".encode() * 100
        calls = [c.call("project.create", {"body": {"name": n}}) for n in names[:4]]
        calls += [c.call("settings.get"), c.call("job.list"), c.call("model.list")]

        async def roundtrip():
            s = await c.open("file.upload", {"name": f"c{i}.txt", "mime": "text/plain"})
            await s.send_data(txt)
            s.end()
            rec = await s.result()
            g = await c.open("file.get", {"fid": rec["id"]})
            _, datas, kind, _ = await g.until_done()
            return kind, b"".join(datas) == txt

        calls.append(roundtrip())
        out = await asyncio.gather(*calls)
        assert [o["name"] for o in out[:4]] == names[:4]
        assert out[-1] == ("end", True)
        return len(out)

    totals = await asyncio.gather(*(worker(i, c) for i, c in enumerate(clients)))
    assert sum(totals) == 16 * 8
    listed = await clients[0].call("project.list")
    assert len(listed) == 16 * 4
    assert len({p["name"] for p in listed}) == 64
    for c in clients:
        await c.close()
    await wait_until(lambda: not snp.server.conns)


# ================================================================== idempotency
async def test_idempotent_replay_executes_once(snp):
    c = await snp.client()
    a = await c.call("project.create", {"body": {"name": "Once"}}, idem="idem-0001")
    b = await c.call("project.create", {"body": {"name": "Once"}}, idem="idem-0001")
    assert a == b
    assert len(await c.call("project.list")) == 1
    d = await c.call("project.create", {"body": {"name": "Once"}}, idem="idem-0002")
    assert d["id"] != a["id"] and len(await c.call("project.list")) == 2
    without = [await c.call("project.create", {"body": {"name": "N"}}) for _ in range(2)]
    assert without[0]["id"] != without[1]["id"]


async def test_idem_key_for_a_different_operation_conflicts(snp):
    c = await snp.client()
    await c.call("project.create", {"body": {"name": "X"}}, idem="idem-shared")
    with pytest.raises(sc.SnpError) as e:
        await c.call("collection.create", {"body": {"name": "Y"}}, idem="idem-shared")
    assert e.value.code == "conflict" and e.value.status == 409 and e.value.retryable is False
    assert await c.call("collection.list") == []


async def test_in_flight_duplicate_attaches_to_the_running_execution(snp):
    probe = Probe(delay=0.4)
    patch_op(snp, "tag.list", probe)
    snp.server.catalogue.get("tag.list").method = "POST"  # idempotency applies to non-GET operations
    c = await snp.client()
    s1 = await c.open("tag.list", idem="idem-flight")
    await asyncio.sleep(0.1)
    s2 = await c.open("tag.list", idem="idem-flight")
    r1, r2 = await asyncio.gather(s1.result(), s2.result())
    assert r1 == r2 == {"n": 0} and probe.calls == 1
    assert await c.call("tag.list", idem="idem-flight") == {"n": 0} and probe.calls == 1


async def test_retryable_failures_are_not_stored_but_final_errors_are(snp):
    snp.server.catalogue.get("tag.list").method = "POST"
    probe = Probe(fail=[HTTPException(503, "busy")])
    patch_op(snp, "tag.list", probe)
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("tag.list", idem="idem-retry")
    assert e.value.code == "resource_exhausted" and e.value.retryable is True
    assert await c.call("tag.list", idem="idem-retry") == {"n": 1} and probe.calls == 2  # re-executed
    probe2 = Probe(fail=[HTTPException(400, "nope")])
    patch_op(snp, "tag.list", probe2)
    for _ in range(2):
        with pytest.raises(sc.SnpError) as e:
            await c.call("tag.list", idem="idem-final")
        assert e.value.code == "bad_request" and e.value.info["message"] == "nope"
    assert probe2.calls == 1  # the recorded error is replayed


async def test_get_operations_and_unnegotiated_idempotency_ignore_the_key(snp):
    probe = Probe()
    patch_op(snp, "tag.list", probe)  # GET: not recorded
    c = await snp.client()
    await c.call("tag.list", idem="idem-get")
    await c.call("tag.list", idem="idem-get")
    assert probe.calls == 2
    snp.server.catalogue.get("tag.list").method = "POST"
    c2 = await snp.client(features=["events"])  # idempotency not offered by the client
    assert "idempotency" not in c2.features
    await c2.call("tag.list", idem="idem-off")
    await c2.call("tag.list", idem="idem-off")
    assert probe.calls == 4


async def test_retry_after_connection_loss_receives_the_original_outcome(snp):
    snp.server.catalogue.get("tag.list").method = "POST"
    probe = Probe(delay=0.5)
    patch_op(snp, "tag.list", probe)
    a = await snp.client()
    await a.open("tag.list", idem="idem-reconnect")
    await asyncio.sleep(0.1)
    a.raw.writer.transport.abort()
    b = await snp.client()
    assert await b.call("tag.list", idem="idem-reconnect") == {"n": 0}
    assert probe.calls == 1 and probe.cancelled == 0


async def test_cancelled_stream_does_not_abort_an_idempotent_execution(snp):
    snp.server.catalogue.get("tag.list").method = "POST"
    probe = Probe(delay=0.4)
    patch_op(snp, "tag.list", probe)
    c = await snp.client()
    s = await c.open("tag.list", idem="idem-cancel")
    await asyncio.sleep(0.1)
    s.cancel()
    assert (await s.until_done())[3]["code"] == "cancelled"
    assert await c.call("tag.list", idem="idem-cancel") == {"n": 0} and probe.calls == 1


@pytest.mark.limits(idem_ttl_s=0)
async def test_idem_ttl_zero_keeps_nothing(snp):
    c = await snp.client()
    a = await c.call("project.create", {"body": {"name": "T"}}, idem="idem-ttl")
    b = await c.call("project.create", {"body": {"name": "T"}}, idem="idem-ttl")
    assert a["id"] != b["id"]


@pytest.mark.limits(idem_max=3)
async def test_idempotency_table_is_lru_bounded(snp):
    c = await snp.client()
    first = await c.call("project.create", {"body": {"name": "L0"}}, idem="k0")
    for i in range(1, 5):
        await c.call("project.create", {"body": {"name": f"L{i}"}}, idem=f"k{i}")
    assert len(snp.server.idem) <= 3
    again = await c.call("project.create", {"body": {"name": "L0"}}, idem="k0")
    assert again["id"] != first["id"]  # evicted, so it executed again


# ================================================================== deadlines and cancellation
async def test_explicit_deadline_terminates_the_operation(snp):
    probe = Probe(delay=5)
    patch_op(snp, "tag.list", probe)
    c = await snp.client()
    s = await c.open("tag.list", deadline_ms=150)
    _, _, kind, val = await s.until_done()
    assert val["code"] == "deadline_exceeded" and val["status"] == 504 and val["retryable"] is True
    assert probe.cancelled == 1
    assert (await c.call("settings.get"))


@pytest.mark.limits(default_unary_deadline_s=0.3)
async def test_default_unary_deadline_but_not_for_long_running_operations(snp):
    patch_op(snp, "tag.list", Probe(delay=5))
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("tag.list")
    assert e.value.code == "deadline_exceeded"
    long_op = Probe(delay=0.6)
    patch_op(snp, "llm.status", long_op)  # 'llm.' operations are long-running in the catalogue sense
    assert await c.call("llm.status") == {"n": 0}


async def test_cancel_stops_a_unary_operation(snp):
    probe = Probe(delay=30)
    patch_op(snp, "tag.list", probe)
    c = await snp.client()
    s = await c.open("tag.list")
    await asyncio.sleep(0.1)
    s.cancel()
    _, _, kind, val = await s.until_done()
    assert val["code"] == "cancelled" and val["status"] == 499
    await wait_until(lambda: probe.cancelled == 1)


async def test_unexpected_exception_is_internal_and_does_not_leak_details(snp):
    async def boom(st: AppState):
        raise RuntimeError("secret path /home/x/y")

    patch_op(snp, "tag.list", boom)
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("tag.list")
    assert e.value.code == "internal" and e.value.status == 500 and "secret" not in e.value.info["message"]


# ================================================================== liveness
@pytest.mark.limits(ping_interval_ms=200, idle_timeout_ms=700)
async def test_server_pings_and_a_responsive_client_stays_connected(snp):
    c = await snp.client()
    await asyncio.sleep(1.6)
    assert len(c.pings) >= 3 and all(len(p) == 8 for p in c.pings)
    assert not c.closed.is_set()
    assert (await c.call("settings.get"))


@pytest.mark.limits(ping_interval_ms=200, idle_timeout_ms=700)
async def test_silent_client_is_dropped_with_idle_timeout(snp):
    c = await snp.client()
    c.auto_pong = False
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(c.goaway_event.wait(), 3)
    assert c.goaway["code"] == "idle_timeout"
    assert 0.5 < asyncio.get_running_loop().time() - t0 < 2.0
    await asyncio.wait_for(c.closed.wait(), 3)
    assert len(c.pings) >= 1
    await wait_until(lambda: not snp.server.conns)


@pytest.mark.limits(ping_interval_ms=200, idle_timeout_ms=700)
async def test_any_client_frame_counts_as_liveness(snp):
    c = await snp.client()
    c.auto_pong = False
    for _ in range(8):
        c.raw.send(sc.PING, 0, bytes(8))
        await asyncio.sleep(0.25)
    assert not c.closed.is_set() and c.goaway is None
    assert len(c.pongs) >= 7


@pytest.mark.limits(ping_interval_ms=200, idle_timeout_ms=700)
async def test_idle_timeout_cancels_streams(snp):
    probe = Probe(delay=30)
    patch_op(snp, "tag.list", probe)
    c = await snp.client()
    c.auto_pong = False
    await c.open("tag.list")
    await wait_until(lambda: probe.cancelled == 1, timeout=4)


# ================================================================== shutdown
async def test_graceful_shutdown_sends_goaway_and_drains(snp):
    probe = Probe(delay=0.4)
    patch_op(snp, "llm.status", probe)
    a, b = await snp.client(), await snp.client()
    sub = await a.open("event.subscribe")
    assert (await sub.recv())[1]["type"] == "hello"
    running = await b.open("llm.status")
    await asyncio.sleep(0.1)
    stop = asyncio.create_task(snp.server.stop())
    await asyncio.wait_for(a.goaway_event.wait(), 3)
    await asyncio.wait_for(b.goaway_event.wait(), 3)
    for c in (a, b):
        assert c.goaway["code"] == "shutdown"
    assert a.goaway["last_stream_id"] == 1 and b.goaway["last_stream_id"] == 1
    assert (await sub.until_done())[2] == "end"          # subscriptions end cleanly
    assert await running.result() == {"n": 0}            # in-flight work drains
    await asyncio.wait_for(a.closed.wait(), 3)
    await asyncio.wait_for(b.closed.wait(), 3)
    await stop
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await sc.Raw.connect(snp.host, snp.port)


@pytest.mark.limits(drain_s=0.3)
async def test_shutdown_gives_up_on_stuck_streams_after_the_drain_time(snp):
    patch_op(snp, "llm.status", Probe(delay=30))
    c = await snp.client()
    s = await c.open("llm.status")
    await asyncio.sleep(0.1)
    t0 = asyncio.get_running_loop().time()
    await snp.server.stop()
    assert asyncio.get_running_loop().time() - t0 < 3
    await asyncio.wait_for(c.closed.wait(), 3)
    assert c.goaway["code"] == "shutdown"
    assert s.done is None or s.done[0] in ("error", "eof")


async def test_new_open_after_goaway_is_disconnected(snp):
    probe = Probe(delay=1.0)
    patch_op(snp, "llm.status", probe)
    c = await snp.client()
    await c.open("llm.status")
    await asyncio.sleep(0.05)
    stop = asyncio.create_task(snp.server.stop())
    await asyncio.wait_for(c.goaway_event.wait(), 3)
    await c.open("settings.get")  # data after GOAWAY: disconnected without a reply
    await asyncio.wait_for(c.closed.wait(), 3)
    await stop
