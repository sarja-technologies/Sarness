"""SNP streaming operations: chat.generate, agent.completion (stub model), event.subscribe, files."""

import asyncio
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

import snp_client as sc
from sarness_engine import storage as storage_mod

pytestmark = pytest.mark.asyncio


async def wait_until(pred, timeout=5.0, step=0.02):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not reached in time")


def snp_tasks():
    return [t for t in asyncio.all_tasks() if not t.done()
            and "/sarness_engine/snp/" in getattr(getattr(t.get_coro(), "cr_code", None), "co_filename", "")
            and t.get_coro().__qualname__ in ("Connection._run_stream", "event_subscribe", "chat_generate",
                                              "Connection._keepalive_loop", "SnpServer._serve")]


def chunk(content=None, reasoning=None, reasoning_key="reasoning_content", finish=None, **extra):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta[reasoning_key] = reasoning
    return {"choices": [{"delta": delta, "finish_reason": finish}], **extra}


class FakeModel:
    """Replaces ``stream_openai``. ``script`` is a list of chunks, or a callable returning an async iterator."""

    def __init__(self, script=None, block_after=None):
        self.script = script or []
        self.block_after = block_after  # number of chunks after which the stream hangs forever
        self.bodies = []
        self.yielded = 0
        self.cancelled = False
        self.closed = False

    async def __call__(self, base_url, api_key, body):
        self.bodies.append(body)
        try:
            for i, c in enumerate(self.script):
                if self.block_after is not None and i == self.block_after:
                    await asyncio.Event().wait()
                self.yielded += 1
                yield c
                await asyncio.sleep(0)
            if self.block_after is not None and self.block_after >= len(self.script):
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True


@pytest.fixture
def model(monkeypatch):
    m = FakeModel()
    monkeypatch.setattr("sarness_engine.chat.service.stream_openai", m)
    return m


async def setup_chat(c, **conv):
    await c.call("settings.patch", {"patch": {"llm_endpoints": [
        {"id": "e1", "name": "mock", "base_url": "http://127.0.0.1:1/v1", "api_key": "k", "models": ["m"]}]}})
    return await c.call("chat.conversation.create", {"body": {"model_ref": "endpoint:e1:m", **conv}})


async def drain_events(s, timeout=10):
    events = []
    while True:
        kind, v = await s.recv(timeout)
        if kind != "msg":
            return events, kind, v
        events.append(v)


# ================================================================== chat.generate
async def test_generate_streams_events_and_reasoning(snp, model):
    model.script = [chunk(reasoning="Let me "), chunk(reasoning="think.", reasoning_key="reasoning"),
                    chunk(content="Hello"), chunk(content=" world", finish="stop"),
                    {"choices": [], "usage": {"completion_tokens": 4, "prompt_tokens": 2}}]
    c = await snp.client()
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "hi there"}})
    events, kind, end = await drain_events(s)
    assert kind == "end" and end == {}
    assert [e["type"] for e in events] == ["user_message", "title", "assistant_start"] + ["delta"] * 4 + ["done"]
    deltas = [e for e in events if e["type"] == "delta"]
    assert [d["reasoning"] for d in deltas] == ["Let me ", "think.", "", ""]
    assert [d["content"] for d in deltas] == ["", "", "Hello", " world"]
    done = events[-1]["message"]
    assert done["content"] == "Hello world" and done["status"] == "complete"
    assert done["reasoning"] == "Let me think."
    assert events[0]["message"]["role"] == "user" and events[1]["title"] == "hi there"
    got = await c.call("chat.conversation.get", {"cid": conv["id"]})
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert not snp.st.chat._stops


async def test_generate_prestart_failures_are_error_frames_without_msg(snp, model):
    c = await snp.client()
    conv = await c.call("chat.conversation.create", {"body": {"title": "NoModel"}})
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "hi"}})
    msgs, _, kind, val = await s.until_done()
    assert msgs == [] and kind == "error" and val["code"] == "bad_request" and val["status"] == 400
    s = await c.open("chat.generate", {"cid": "c_nope", "body": {"content": "hi"}})
    msgs, _, kind, val = await s.until_done()
    assert msgs == [] and val["code"] == "not_found" and val["status"] == 404
    s = await c.open("chat.generate", {"cid": conv["id"]})  # body missing
    _, _, kind, val = await s.until_done()
    assert val["code"] == "bad_request"
    conv2 = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv2["id"], "body": {"content": "   "}})
    _, _, kind, val = await s.until_done()
    assert val["code"] == "bad_request" and "empty" in val["message"]


async def test_second_generate_in_same_conversation_conflicts(snp, model):
    model.script, model.block_after = [chunk(content="a")], 1
    c = await snp.client()
    conv = await setup_chat(c)
    s1 = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "one"}})
    while True:
        kind, v = await s1.recv()
        if kind == "msg" and v["type"] == "delta":
            break
    s2 = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "two"}})
    _, _, kind, val = await s2.until_done()
    assert val["code"] == "conflict" and val["status"] == 409
    s1.cancel()
    assert (await s1.until_done())[3]["code"] == "cancelled"


async def test_cancel_mid_stream_persists_stopped_and_stops_model(snp, model):
    model.script, model.block_after = [chunk(content="par"), chunk(content="tial")], 2
    c = await snp.client()
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "go"}})
    seen = ""
    while seen != "partial":
        kind, v = await s.recv()
        if kind == "msg" and v["type"] == "delta":
            seen += v["content"]
    s.cancel()
    _, _, kind, val = await s.until_done()
    assert kind == "error" and val["code"] == "cancelled" and val["status"] == 499 and val["retryable"] is False
    await wait_until(lambda: model.cancelled and model.closed)
    got = await c.call("chat.conversation.get", {"cid": conv["id"]})
    last = got["messages"][-1]
    assert last["role"] == "assistant" and last["status"] == "stopped" and last["content"] == "partial"
    assert not snp.st.chat._stops
    await c.call("settings.get")  # the connection survives a stream-level cancel
    await wait_until(lambda: not snp.server.conns or all(not x.streams for x in snp.server.conns))


async def test_connection_loss_stops_generation_and_leaks_nothing(snp, model):
    model.script, model.block_after = [chunk(content="abc")], 1
    c = await snp.client()
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "go"}})
    while True:
        kind, v = await s.recv()
        if kind == "msg" and v["type"] == "delta":
            break
    c.raw.writer.transport.abort()  # no CANCEL, no goodbye
    await wait_until(lambda: model.cancelled)
    await wait_until(lambda: not snp.server.conns)
    c2 = await snp.client()
    last = (await c2.call("chat.conversation.get", {"cid": conv["id"]}))["messages"][-1]
    assert last["status"] == "stopped" and last["content"] == "abc"
    assert not snp.st.chat._stops
    await c2.close()
    await wait_until(lambda: not snp.server.conns)
    await wait_until(lambda: not [t for t in snp_tasks() if t.get_coro().__qualname__ != "SnpServer._serve"])


async def test_backpressure_reaches_the_model_stream(snp, model):
    model.script = [chunk(content="x" * 1000) for _ in range(3000)] + [chunk(content="", finish="stop")]
    c = await snp.client()
    c.auto_credit = False  # consume frames but withhold WINDOW updates
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "go"}})
    got = 0
    try:
        while True:
            kind, v = await s.recv(0.7)
            assert kind == "msg"
            got += 1
    except asyncio.TimeoutError:
        pass
    frozen = model.yielded
    # ~256 KiB of credit / ~1.1 KB per event: the server may only have pulled a few hundred chunks.
    assert 150 < frozen < 400, frozen
    await asyncio.sleep(0.5)
    assert model.yielded == frozen, "server kept consuming the model while the client withheld credit"
    # Releasing credit resumes the flow.
    c.grant(s.id, 262144)
    await wait_until(lambda: model.yielded > frozen + 100)
    c.auto_credit = True
    c.grant(s.id, 262144)
    last = None
    while True:
        kind, v = await s.recv(10)
        if kind != "msg":
            break
        last = v
    assert kind == "end", v
    assert model.yielded == len(model.script)
    # The finished reply (3 MB) cannot fit a 256 KiB window: 'done' arrives without the text.
    assert last["type"] == "done" and last["message"]["truncated"] is True and last["message"]["content"] == ""
    stored = snp.st.db.one("SELECT status, content FROM messages WHERE id = ?", [last["message"]["id"]])
    assert stored["status"] == "complete" and len(stored["content"]) == 3_000_000
    # And a result that does not fit max_frame is a stream error, not an invalid frame.
    with pytest.raises(sc.SnpError) as e:
        await c.call("chat.conversation.get", {"cid": conv["id"]})
    assert e.value.code == "too_large"


@pytest.mark.limits(stall_s=0.5)
async def test_stalled_stream_terminates_with_error_stalled(snp, model):
    model.script = [chunk(content="y" * 1000) for _ in range(3000)]
    c = await snp.client()
    c.auto_credit = False
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "go"}})
    err = None
    while True:
        kind, v = await s.recv(5)
        if kind == "error":
            err = v
            break
    assert err["code"] == "stalled" and err["status"] == 504 and err["retryable"] is True
    await wait_until(lambda: model.closed)  # the generator is thrown CancelledError; its model stream is closed
    last = (await c.call("chat.conversation.get", {"cid": conv["id"]}))["messages"][-1]
    assert last["status"] == "stopped"
    assert not snp.st.chat._stops


async def test_generate_idem_reuse_is_a_conflict(snp, model):
    model.script = [chunk(content="ok", finish="stop")]
    c = await snp.client()
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "one"}}, idem="key-gen-1")
    assert (await s.until_done())[2] == "end"
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "two"}}, idem="key-gen-1")
    _, _, kind, val = await s.until_done()
    assert val["code"] == "conflict"
    got = await c.call("chat.conversation.get", {"cid": conv["id"]})
    assert [m["content"] for m in got["messages"] if m["role"] == "user"] == ["one"]


async def test_generate_deadline(snp, model):
    model.script, model.block_after = [chunk(content="a")], 1
    c = await snp.client()
    conv = await setup_chat(c)
    s = await c.open("chat.generate", {"cid": conv["id"], "body": {"content": "go"}}, deadline_ms=300)
    _, _, kind, val = await s.until_done()
    assert val["code"] == "deadline_exceeded" and val["status"] == 504 and val["retryable"] is True
    await wait_until(lambda: model.cancelled)
    assert (await c.call("chat.conversation.get", {"cid": conv["id"]}))["messages"][-1]["status"] == "stopped"


# ================================================================== agent.completion
TOOL = {"type": "function", "function": {"name": "read_file", "description": "x",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


async def test_agent_completion_tool_call_and_reasoning(snp, model):
    model.script = [chunk(reasoning="hmm"), chunk(content="Looking. "),
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a",
                                                            "function": {"name": "read_file", "arguments": ""}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a.txt"}'}}]}}]},
                    chunk(finish="tool_calls")]
    c = await snp.client()
    await setup_chat(c)
    listed = await c.call("chat.conversation.list")
    body = {"model_ref": "endpoint:e1:m", "messages": [{"role": "user", "content": "read a.txt"}], "tools": [TOOL]}
    s = await c.open("agent.completion", {"body": body})
    msgs, _, kind, end = await s.until_done()
    assert kind == "end"
    types = [m["type"] for m in msgs]
    assert types[0] == "delta" and types[-1] == "done" and "tool_call" in types
    assert msgs[0]["reasoning"] == "hmm"
    call = msgs[-1]["message"]["tool_calls"][0]
    assert call["id"] == "call_a" and json.loads(call["function"]["arguments"]) == {"path": "a.txt"}
    assert model.bodies[-1]["tools"][0]["function"]["name"] == "read_file"
    assert await c.call("chat.conversation.list") == listed  # stateless: nothing was stored


@pytest.mark.parametrize("body,code", [
    ({"model_ref": "endpoint:e1:m", "messages": []}, "bad_request"),
    ({"model_ref": "endpoint:e1:m", "messages": [{"role": "robot", "content": "x"}]}, "bad_request"),
    ({"messages": [{"role": "user", "content": "x"}]}, "bad_request"),
    ({"model_ref": "endpoint:zzz:m", "messages": [{"role": "user", "content": "x"}]}, "not_found"),
])
async def test_agent_completion_validation(snp, model, body, code):
    c = await snp.client()
    await setup_chat(c)
    s = await c.open("agent.completion", {"body": body})
    msgs, _, kind, val = await s.until_done()
    assert msgs == [] and kind == "error" and val["code"] == code


async def test_agent_completion_cancel(snp, model):
    model.script, model.block_after = [chunk(content="a")], 1
    c = await snp.client()
    await setup_chat(c)
    s = await c.open("agent.completion", {"body": {"model_ref": "endpoint:e1:m", "messages": [{"role": "user", "content": "x"}]}})
    while (await s.recv())[1].get("type") != "delta":
        pass
    s.cancel()
    assert (await s.until_done())[3]["code"] == "cancelled"
    await wait_until(lambda: model.cancelled)


# ================================================================== event.subscribe
async def next_event(s, timeout=3):
    kind, v = await s.recv(timeout)
    assert kind == "msg", (kind, v)
    return v


async def test_subscribe_hello_events_and_shape(snp):
    c = await snp.client()
    s = await c.open("event.subscribe")
    hello = await next_event(s)
    assert hello["type"] == "hello" and hello["data"]["api_version"]
    assert snp.st.bus.subscriber_count == 1
    snp.st.bus.publish("models.changed", {"model_id": "m1"})
    ev = await next_event(s)
    assert ev["type"] == "models.changed" and ev["data"] == {"model_id": "m1"} and isinstance(ev["ts"], float)
    assert "dropped" not in ev
    # An engine-originated event (project create publishes projects.changed) arrives too.
    await c.call("project.create", {"body": {"name": "Evt"}})
    assert (await next_event(s))["type"] == "projects.changed"
    s.cancel()
    assert (await s.until_done())[3]["code"] == "cancelled"
    await wait_until(lambda: snp.st.bus.subscriber_count == 0)


async def test_subscribe_topic_filter_and_control_messages(snp):
    c = await snp.client()
    s = await c.open("event.subscribe", {"topics": ["job."]})
    await next_event(s)
    bus = snp.st.bus
    bus.publish("models.changed", {})
    bus.publish("job.done", {"id": 1})
    assert (await next_event(s))["type"] == "job.done"
    s.send_msg({"subscribe": ["models.changed"]})
    await asyncio.sleep(0.1)
    bus.publish("models.changed", {"n": 2})
    assert (await next_event(s))["data"] == {"n": 2}
    s.send_msg({"unsubscribe": ["job."]})
    await asyncio.sleep(0.1)
    bus.publish("job.done", {"id": 2})
    bus.publish("models.changed", {"n": 3})
    assert (await next_event(s))["data"] == {"n": 3}  # job.done was filtered out
    # Exact topic names match exactly, prefixes need the trailing dot.
    s.send_msg({"subscribe": ["settings"]})
    await asyncio.sleep(0.1)
    bus.publish("settings.changed", {})
    bus.publish("models.changed", {"n": 4})
    assert (await next_event(s))["data"] == {"n": 4}
    s.end()
    kind, val = (await s.recv())[0:2]
    assert kind == "end"
    await wait_until(lambda: bus.subscriber_count == 0)


async def test_subscribe_all_topics_can_be_muted(snp):
    c = await snp.client()
    s = await c.open("event.subscribe")
    await next_event(s)
    s.send_msg({"unsubscribe": ["system.stats", "job."]})
    await asyncio.sleep(0.1)
    snp.st.bus.publish("system.stats", {})
    snp.st.bus.publish("job.progress", {})
    snp.st.bus.publish("llm.status", {"k": 1})
    assert (await next_event(s))["type"] == "llm.status"


async def test_subscribe_bad_control_message_ends_only_that_stream(snp):
    c = await snp.client()
    s = await c.open("event.subscribe")
    await next_event(s)
    s.send_msg({"subscribe": "job."})
    _, _, kind, val = await s.until_done()
    assert kind == "error" and val["code"] == "bad_request"
    await c.call("settings.get")
    await wait_until(lambda: snp.st.bus.subscriber_count == 0)


async def test_subscribe_slow_consumer_drops_telemetry_and_counts(snp):
    c = await snp.client()
    c.auto_credit = False
    s = await c.open("event.subscribe", {"topics": ["job.", "models."]})
    await next_event(s)
    blob = "z" * 20000
    total = 60
    for i in range(total):
        snp.st.bus.publish("job.preview", {"i": i, "image": blob})
    await asyncio.sleep(0.5)
    received = 0
    while True:
        try:
            ev = await next_event(s, 0.3)
        except asyncio.TimeoutError:
            break
        assert ev["type"] == "job.preview"
        received += 1
    assert 5 < received < total  # ~256 KiB of credit fits roughly a dozen of these
    c.grant(s.id, 262144)
    snp.st.bus.publish("models.changed", {"after": True})
    ev = await next_event(s)
    assert ev["type"] == "models.changed" and ev["dropped"] == total - received
    snp.st.bus.publish("models.changed", {"again": True})
    assert "dropped" not in await next_event(s)  # the counter was reset by the delivery


@pytest.mark.limits(stall_s=0.4)
async def test_subscribe_non_telemetry_event_stalls_the_stream(snp):
    c = await snp.client()
    c.auto_credit = False
    s = await c.open("event.subscribe", {"topics": ["models."]})
    await next_event(s)
    for i in range(30):
        snp.st.bus.publish("models.changed", {"image": "q" * 20000})
    while True:
        kind, v = await s.recv(5)
        if kind == "error":
            break
    assert v["code"] == "stalled"
    await wait_until(lambda: snp.st.bus.subscriber_count == 0)


async def test_subscribe_survives_many_events_with_credit(snp):
    c = await snp.client()
    s = await c.open("event.subscribe", {"topics": ["models."]})
    await next_event(s)
    for i in range(500):
        snp.st.bus.publish("models.changed", {"i": i})
    got = [(await next_event(s))["data"]["i"] for _ in range(500)]
    assert got == list(range(500))


# ================================================================== files
def make_png(size=16, color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


async def upload(c, data: bytes, name="x.png", step=60000, **args):
    s = await c.open("file.upload", {"name": name, **args})
    for i in range(0, len(data), step):
        await s.send_data(data[i:i + step])
    s.end()
    return await s.result()


async def download(c, fid, **args):
    s = await c.open("file.get", {"fid": fid, **args})
    msgs, datas, kind, val = await s.until_done(20)
    if kind == "error":
        raise sc.SnpError(val)
    return msgs, datas


async def test_upload_and_download_roundtrip_matches_http(snp):
    c = await snp.client()
    png = make_png(24, "red")
    rec = await upload(c, png, name="../../evil/ref.png", mime="image/png", size=len(png))
    assert rec["mime"] == "image/png" and rec["size"] == len(png) and rec["width"] == 24 and rec["kind"] == "upload"
    assert rec["original_name"] == "ref.png" and rec["url"] == f"/v1/files/{rec['id']}"
    assert rec["sha256"] == hashlib.sha256(png).hexdigest()
    row = snp.st.storage.get(rec["id"])
    assert row["path"].startswith("uploads/") and snp.st.storage.resolve(row["path"]).read_bytes() == png
    msgs, datas = await download(c, rec["id"])
    assert msgs == [{"meta": {"mime": "image/png", "size": len(png), "start": 0, "end": len(png) - 1,
                              "length": len(png), "name": "ref.png"}}]
    assert b"".join(datas) == png
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=snp.app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {snp.token}"}) as h:
        r = await h.get(rec["url"])
        assert r.content == png and r.headers["content-type"] == "image/png"
        # An HTTP upload produces the same kind of record.
        hr = (await h.post("/v1/uploads", files={"file": ("ref.png", png, "image/png")})).json()
        assert {k: hr[k] for k in ("mime", "size", "kind", "width", "height", "original_name", "sha256")} == \
               {k: rec[k] for k in ("mime", "size", "kind", "width", "height", "original_name", "sha256")}


async def test_download_ranges(snp):
    c = await snp.client()
    text = ("0123456789" * 100).encode()
    rec = await upload(c, text, name="t.txt", mime="text/plain")
    for rng, (a, b) in (({"start": 10, "end": 19}, (10, 19)), ({"start": 990}, (990, 999)), ({"start": 0, "end": 0}, (0, 0)),
                        ({"start": 500, "end": 99999}, (500, 999)), ({"start": 999, "end": 999}, (999, 999))):
        msgs, datas = await download(c, rec["id"], range=rng)
        meta = msgs[0]["meta"]
        assert (meta["start"], meta["end"], meta["size"], meta["length"]) == (a, b, 1000, b - a + 1), rng
        assert b"".join(datas) == text[a:b + 1]
    for bad in ({"start": 1000}, {"start": 5000, "end": 6000}):
        with pytest.raises(sc.SnpError) as e:
            await download(c, rec["id"], range=bad)
        assert e.value.code == "bad_request" and e.value.status == 416
    for bad in ({"start": -1}, {"start": 5, "end": 2}, {"end": 5}, {"start": "a"}, [1, 2], 7):
        with pytest.raises(sc.SnpError) as e:
            await download(c, rec["id"], range=bad)
        assert e.value.code == "bad_request" and e.value.status == 400


async def test_download_errors(snp):
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await download(c, "f_missing")
    assert e.value.code == "not_found" and e.value.status == 404
    with pytest.raises(sc.SnpError) as e:
        await c.call("file.get", {})
    assert e.value.code == "bad_request"
    rec = await upload(c, make_png())
    snp.st.storage.resolve(snp.st.storage.get(rec["id"])["path"]).unlink()
    with pytest.raises(sc.SnpError) as e:
        await download(c, rec["id"])
    assert e.value.code == "not_found" and e.value.status == 410


async def test_large_multiframe_transfer_respects_frame_and_window_limits(snp):
    c = await snp.client(max_frame=65536)
    assert c.limits["max_frame"] == 65536
    body = (b"0123456789abcdef" * 4096 * 48)  # 3 MiB of text, far above the 256 KiB window
    rec = await upload(c, body, name="big.txt", mime="text/plain", step=65000)
    assert rec["size"] == len(body)
    msgs, datas = await download(c, rec["id"])
    assert len(datas) > 20 and max(map(len, datas)) <= 65536
    assert hashlib.sha256(b"".join(datas)).hexdigest() == hashlib.sha256(body).hexdigest()
    assert msgs[0]["meta"]["size"] == len(body)
    # A range spanning several frames in the middle.
    msgs, datas = await download(c, rec["id"], range={"start": 100000, "end": 999999})
    assert b"".join(datas) == body[100000:1000000] and len(datas) > 10


async def test_download_honours_credit_and_can_be_cancelled(snp):
    c = await snp.client()
    body = b"abcdefgh" * 400000  # 3.2 MB
    rec = await upload(c, body, name="b.txt", mime="text/plain", step=65000)
    c.auto_credit = False
    s = await c.open("file.get", {"fid": rec["id"]})
    got = 0
    try:
        while True:
            kind, v = await s.recv(0.7)
            if kind == "data":
                got += len(v)
    except asyncio.TimeoutError:
        pass
    assert got <= 262144 and got > 100000, got  # stream window, not the whole file
    s.cancel()
    _, _, kind, val = await s.until_done()
    assert val["code"] == "cancelled"
    await c.call("settings.get")


async def test_upload_limit_enforced_incrementally(snp, monkeypatch):
    monkeypatch.setattr(storage_mod, "MAX_UPLOAD_BYTES", 100_000)
    c = await snp.client()
    before = snp.st.db.scalar("SELECT COUNT(*) FROM files")
    s = await c.open("file.upload", {"name": "big.txt", "size": 5})  # a lying 'size' must not be trusted
    sent = 0
    err = None
    for _ in range(30):
        try:
            await s.send_data(b"a" * 20000)
        except TimeoutError:
            break
        sent += 20000
        await asyncio.sleep(0.02)  # client and server share this loop: let the server run
        if not s.queue.empty():
            kind, v = await s.recv()
            if kind == "error":
                err = v
                break
    if err is None:
        err = (await s.until_done())[3]
    assert err["code"] == "too_large" and err["status"] == 413
    assert sent <= 160_000, f"server accepted {sent} octets before refusing"
    await asyncio.sleep(0.1)
    assert snp.st.db.scalar("SELECT COUNT(*) FROM files") == before
    assert not list(snp.st.cfg.uploads_dir.iterdir()) and not list(snp.st.cfg.tmp_dir.glob("snp-upload-*"))
    # Late DATA for the closed stream is discarded and its credit returned: new uploads still work.
    s.client.raw.send(sc.DATA, s.id, b"b" * 1000)
    rec = await upload(c, b"small text", name="s.txt", mime="text/plain")
    assert rec["size"] == 10
    # Declaring an oversized upload up front fails immediately.
    s = await c.open("file.upload", {"size": 10_000_000})
    assert (await s.until_done())[3]["code"] == "too_large"


async def test_upload_failures_leave_no_record(snp):
    c = await snp.client()
    before = snp.st.db.scalar("SELECT COUNT(*) FROM files")
    # size mismatch
    s = await c.open("file.upload", {"name": "a.txt", "size": 100})
    await s.send_data(b"only ten..")
    s.end()
    with pytest.raises(sc.SnpError) as e:
        await s.result()
    assert e.value.code == "bad_request"
    # unsupported type
    s = await c.open("file.upload", {"name": "a.bin"})
    await s.send_data(bytes(range(256)) * 4)
    s.end()
    with pytest.raises(sc.SnpError) as e:
        await s.result()
    assert e.value.code == "bad_request" and "unsupported" in e.value.info["message"]
    # corrupt image
    s = await c.open("file.upload", {"name": "a.png", "mime": "image/png"})
    await s.send_data(b"\x89PNG\r\n\x1a\n" + b"junk" * 50)
    s.end()
    with pytest.raises(sc.SnpError):
        await s.result()
    # bad arguments
    for args in ({"size": -1}, {"name": 5}, {"mime": []}):
        s = await c.open("file.upload", args)
        assert (await s.until_done())[3]["code"] == "bad_request"
    # MSG is not valid on an upload
    s = await c.open("file.upload", {})
    s.send_msg({"x": 1})
    assert (await s.until_done())[3]["code"] == "bad_request"
    assert snp.st.db.scalar("SELECT COUNT(*) FROM files") == before
    assert not list(snp.st.cfg.uploads_dir.iterdir()) and not list(snp.st.cfg.tmp_dir.glob("snp-upload-*"))


async def test_interrupted_upload_by_cancel_or_disconnect_is_discarded(snp):
    c = await snp.client()
    before = snp.st.db.scalar("SELECT COUNT(*) FROM files")
    s = await c.open("file.upload", {"name": "a.txt"})
    await s.send_data(b"x" * 50000)
    await wait_until(lambda: list(snp.st.cfg.tmp_dir.glob("snp-upload-*")))
    s.cancel()
    assert (await s.until_done())[3]["code"] == "cancelled"
    await wait_until(lambda: not list(snp.st.cfg.tmp_dir.glob("snp-upload-*")))
    c2 = await snp.client()
    s = await c2.open("file.upload", {"name": "a.txt"})
    await s.send_data(b"x" * 50000)
    await wait_until(lambda: list(snp.st.cfg.tmp_dir.glob("snp-upload-*")))
    c2.raw.writer.transport.abort()
    await wait_until(lambda: not list(snp.st.cfg.tmp_dir.glob("snp-upload-*")))
    assert snp.st.db.scalar("SELECT COUNT(*) FROM files") == before
    assert not list(snp.st.cfg.uploads_dir.iterdir())


async def test_upload_flow_control_overrun_is_a_protocol_error(snp):
    c = await snp.client()
    s = await c.open("file.upload", {"name": "a.txt"})
    for _ in range(3):  # 3 x 100 KB > the 262 144 octet stream window, sent without waiting for WINDOW
        c.raw.send(sc.DATA, s.id, b"t" * 100_000)
    await c.raw.flush()
    await asyncio.wait_for(c.goaway_event.wait(), 5)
    assert c.goaway["code"] == "protocol_error"
    await asyncio.wait_for(c.closed.wait(), 5)
    await wait_until(lambda: not list(snp.st.cfg.tmp_dir.glob("snp-upload-*")))
