"""/v1/agent/completions: stateless tool-calling inference proxy (mock OpenAI-compatible model server)."""

import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from sarness_engine.app import create_app
from sarness_engine.config import load_config, save_config

TOOL = {"type": "function", "function": {"name": "read_file", "description": "x",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


def _mock_model() -> FastAPI:
    app = FastAPI()
    app.state.bodies = []

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        app.state.bodies.append(body)

        def sse(obj):
            return f"data: {json.dumps(obj)}\n\n"

        async def gen():
            last = body["messages"][-1]
            if last["role"] == "tool":
                yield sse({"choices": [{"delta": {"content": "done: " + last["content"]}, "finish_reason": None}]})
                yield sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}})
            else:  # tool call split over several chunks, like real servers do
                yield sse({"choices": [{"delta": {"content": "Looking. "}}]})
                yield sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": ""}}]}}]})
                yield sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]}}]})
                yield sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]})
                yield sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


@pytest.fixture(scope="module")
def model_server():
    app = _mock_model()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    app.url = f"http://127.0.0.1:{port}/v1"
    yield app
    server.should_exit = True
    t.join(5)


@pytest.fixture
def client(tmp_path, model_server):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    save_config(cfg)
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        r = c.patch("/v1/settings", json={"llm_endpoints": [
            {"id": "e1", "name": "mock", "base_url": model_server.url, "api_key": "k", "models": ["m"]}]})
        assert r.status_code == 200, r.text
        yield c


def _events(resp):
    return [json.loads(l[5:]) for l in resp.text.splitlines() if l.startswith("data:")]


def test_tool_call_is_assembled_and_tools_are_forwarded(client, model_server):
    r = client.post("/v1/agent/completions", json={
        "model_ref": "endpoint:e1:m", "messages": [{"role": "user", "content": "read a.txt"}], "tools": [TOOL]})
    assert r.status_code == 200, r.text
    ev = _events(r)
    done = ev[-1]
    assert done["type"] == "done" and done["finish_reason"] == "tool_calls"
    call = done["message"]["tool_calls"][0]
    assert call["id"] == "call_a" and call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.txt"}
    sent = model_server.state.bodies[-1]
    assert sent["tools"][0]["function"]["name"] == "read_file" and sent["tool_choice"] == "auto"


def test_tool_result_round_trip(client, model_server):
    msgs = [{"role": "user", "content": "read a.txt"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "type": "function",
                                                                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "hello"}]
    r = client.post("/v1/agent/completions", json={"model_ref": "endpoint:e1:m", "messages": msgs, "tools": [TOOL]})
    done = _events(r)[-1]
    assert done["message"]["content"] == "done: hello" and "tool_calls" not in done["message"]


def test_stateless_nothing_stored(client):
    before = client.get("/v1/chat/conversations").json()
    client.post("/v1/agent/completions", json={"model_ref": "endpoint:e1:m",
                                               "messages": [{"role": "user", "content": "x"}]})
    assert client.get("/v1/chat/conversations").json() == before


@pytest.mark.parametrize("body,msg", [
    ({"model_ref": "endpoint:e1:m", "messages": []}, "non-empty"),
    ({"model_ref": "endpoint:e1:m", "messages": [{"role": "robot", "content": "x"}]}, "role"),
    ({"model_ref": "endpoint:e1:m", "messages": [{"role": "tool", "content": "x"}]}, "tool_call_id"),
    ({"model_ref": "endpoint:e1:m", "messages": [{"role": "user", "content": "x"}], "tools": [{"type": "function"}]}, "each tool"),
    ({"messages": [{"role": "user", "content": "x"}]}, "choose a model"),
    ({"model_ref": "endpoint:zzz:m", "messages": [{"role": "user", "content": "x"}]}, "no longer exists"),
])
def test_validation(client, body, msg):
    r = client.post("/v1/agent/completions", json=body)
    assert r.status_code in (400, 404) and msg in r.json()["detail"]


def test_requires_auth(client):
    assert client.post("/v1/agent/completions", headers={"Authorization": "Bearer no"}, json={}).status_code == 401
