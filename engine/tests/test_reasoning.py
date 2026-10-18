"""Model-emitted reasoning is separated from the answer, never invented, never reclassified."""

import json

import pytest
from fastapi.testclient import TestClient

import sarness_engine.chat.service as chat_service
from sarness_engine.app import create_app
from sarness_engine.chat.reasoning import ThinkSplitter
from sarness_engine.config import load_config, save_config


def run(chunks):
    sp = ThinkSplitter()
    ans, rea = [], []
    for c in chunks:
        a, r = sp.route(c, None)
        ans.append(a)
        rea.append(r)
    a, r = sp.finish()
    ans.append(a)
    rea.append(r)
    return "".join(ans), "".join(rea)


@pytest.mark.parametrize("text,answer,reasoning", [
    ("<think>plan it</think>\nDone.", "Done.", "plan it"),
    ("  \n<think>a\nb</think>Done.", "Done.", "a\nb"),
    ("No thinking here.", "No thinking here.", ""),
    ("Answer mentions <think> tags</think> literally.", "Answer mentions <think> tags</think> literally.", ""),
    ("<think>never closed", "", "never closed"),
    ("<thinking>not our tag</thinking>", "<thinking>not our tag</thinking>", ""),
    ("<think></think>Hi", "Hi", ""),
    ("<think>x</think>y</think>z", "y</think>z", "x"),
    ("<thin", "<thin", ""),
])
def test_splitter_every_chunk_boundary(text, answer, reasoning):
    # The result must not depend on how the stream happens to be chunked.
    assert run([text]) == (answer, reasoning)
    for i in range(len(text) + 1):
        assert run([text[:i], text[i:]]) == (answer, reasoning), i
    assert run(list(text)) == (answer, reasoning)


def test_native_reasoning_disables_splitting():
    sp = ThinkSplitter()
    assert sp.route(None, "native thought") == ("", "native thought")
    # Once the provider exposes reasoning natively, literal tags in the answer are just text.
    assert sp.route("<think>keep</think>", None) == ("<think>keep</think>", "")


@pytest.fixture
def client(tmp_path):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    save_config(cfg)
    with TestClient(create_app(cfg, start_background=False)) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        c.patch("/v1/settings", json={"llm_endpoints": [
            {"id": "t", "name": "T", "base_url": "http://fake.invalid/v1", "api_key": "", "models": ["m"]}]})
        yield c


def _fake(chunks):
    async def stream_openai(base_url, api_key, body):
        for c in chunks:
            yield {"choices": [{"delta": c}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return stream_openai


def _generate(client, text="hi"):
    cid = client.post("/v1/chat/conversations", json={"model_ref": "endpoint:t:m"}).json()["id"]
    r = client.post(f"/v1/chat/conversations/{cid}/generate", json={"content": text})
    assert r.status_code == 200
    events = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")]
    return cid, events


@pytest.mark.parametrize("chunks,answer,reasoning", [
    ([{"reasoning_content": "step 1. "}, {"reasoning_content": "step 2."}, {"content": "Result"}], "Result", "step 1. step 2."),
    ([{"reasoning": "ollama style"}, {"content": "Ok"}], "Ok", "ollama style"),
    ([{"content": "<think>inline "}, {"content": "thought</th"}, {"content": "ink>\nAnswer"}], "Answer", "inline thought"),
    ([{"content": "Plain "}, {"content": "answer"}], "Plain answer", ""),
])
def test_chat_stream_and_persistence(client, monkeypatch, chunks, answer, reasoning):
    monkeypatch.setattr(chat_service, "stream_openai", _fake(chunks))
    cid, events = _generate(client)
    done = next(e for e in events if e["type"] == "done")["message"]
    assert done["content"] == answer and (done["reasoning"] or "") == reasoning
    # What was streamed equals what was stored: nothing synthesised in between.
    assert "".join(e["content"] for e in events if e["type"] == "delta") == answer
    assert "".join(e["reasoning"] for e in events if e["type"] == "delta") == reasoning
    stored = [m for m in client.get(f"/v1/chat/conversations/{cid}").json()["messages"] if m["role"] == "assistant"][0]
    assert stored["content"] == answer and (stored["reasoning"] or "") == reasoning


def test_reasoning_does_not_leak_into_next_turn(client, monkeypatch):
    seen = []

    async def spy(base_url, api_key, body):
        seen.append(json.dumps(body["messages"]))
        yield {"choices": [{"delta": {"reasoning_content": "SECRET-THOUGHT"}}]}
        yield {"choices": [{"delta": {"content": "A"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(chat_service, "stream_openai", spy)
    cid, _ = _generate(client, "one")
    client.post(f"/v1/chat/conversations/{cid}/generate", json={"content": "two"})
    assert "SECRET-THOUGHT" not in seen[1]
