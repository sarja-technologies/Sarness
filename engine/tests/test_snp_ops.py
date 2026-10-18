"""SNP operation catalogue vs. the real route table, and unary operations over the wire."""

import asyncio
import json

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

import snp_client as sc
from sarness_engine.app import create_app
from sarness_engine.config import load_config
from sarness_engine.snp import ops as ops_mod
from sarness_engine.snp.ops import CATALOGUE_PATH, Catalogue, resolve_handler


# ----------------------------------------------------------------------------- catalogue <-> routes
def _walk(routes, prefix=""):
    """Flattens the route table. Newer FastAPI keeps included routers as lazy wrappers."""
    for r in routes:
        if hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes, prefix + r.include_context.prefix)
        elif isinstance(r, (APIRoute, APIWebSocketRoute)):
            yield prefix + r.path, r


def _routes(tmp_path):
    app = create_app(load_config(tmp_path / "data"), start_background=False)
    table = {}
    for path, r in _walk(app.routes):
        for m in (r.methods if isinstance(r, APIRoute) else {"GET"}):
            assert (m, path) not in table, f"duplicate route {m} {path}"
            table[(m, path)] = r.endpoint
    return table


def test_catalogue_and_routes_correspond_exactly(tmp_path):
    """Every route has exactly one op and every op has a route; the handler is the route's endpoint."""
    routes = _routes(tmp_path)
    ops = json.loads(CATALOGUE_PATH.read_text())["ops"]
    names = [o["op"] for o in ops]
    assert len(names) == len(set(names)), "duplicate operation names"
    keys = [(o["method"], o["route"]) for o in ops]
    assert len(keys) == len(set(keys)), "two operations share a route"
    assert set(keys) - set(routes) == set(), "operations without a route"
    assert set(routes) - set(keys) == set(), "routes without an operation (add them to snp/ops.json)"
    for o in ops:
        assert resolve_handler(o["handler"]) is routes[(o["method"], o["route"])], o["op"]


def test_catalogue_loads_and_every_operation_is_available():
    cat = Catalogue()
    broken = {n: o.error for n, o in cat.ops.items() if o.error}
    assert not broken, broken
    modes = {o.mode for o in cat.ops.values()}
    assert modes == {"unary", "stream", "download", "upload", "subscribe"}
    for o in cat.ops.values():
        assert o.mode == "unary" or o.native is not None, o.name


def test_catalogue_params_equal_handler_parameters():
    cat = Catalogue()
    for o in cat.ops.values():
        if o.mode != "unary":
            continue
        bound = sorted(p.name for p in o.spec if p.kind == "arg")
        assert bound == sorted(o.params), f"{o.name}: catalogue {sorted(o.params)} != handler {bound}"


def test_catalogue_is_generic_for_new_routes(tmp_path):
    """A route added later only needs an ops.json entry: the dispatcher binds by signature."""
    from fastapi import Body, Depends
    from sarness_engine.api.deps import AppState, require_auth

    def echo(x: int, y: str = "d", body: dict = Body(default={"k": 1}), st: AppState = Depends(require_auth)):
        return {"x": x, "y": y, "body": body}

    spec = ops_mod._param_specs(echo)
    assert [(p.name, p.kind, p.required) for p in spec] == [("x", "arg", True), ("y", "arg", False),
                                                           ("body", "arg", False), ("st", "state", False)]
    op = ops_mod.OpDef("t.echo", "POST", "/x", "api.x:echo", "unary", ["x", "y", "body"], fn=echo, spec=spec)
    out = asyncio.run(ops_mod.call_unary(op, {"x": "5"}, object()))
    assert out == {"x": 5, "y": "d", "body": {"k": 1}}


# ----------------------------------------------------------------------------- unary smoke
@pytest.mark.asyncio
async def test_conversation_crud_and_import_export(snp):
    c = await snp.client()
    conv = await c.call("chat.conversation.create", {"body": {"title": "Plans"}})
    cid = conv["id"]
    got = await c.call("chat.conversation.get", {"cid": cid})
    assert got["title"] == "Plans"
    await c.call("chat.conversation.update", {"cid": cid, "body": {"pinned": 1}})
    listed = await c.call("chat.conversation.list", {"q": "Plan"})
    assert [x["id"] for x in listed] == [cid] and listed[0]["pinned"] == 1
    exp = await c.call("chat.conversation.export", {"cid": cid})
    imp = await c.call("chat.conversation.import", {"body": exp})
    assert imp["title"] == "Plans" and imp["id"] != cid
    md = await c.call("chat.conversation.export", {"cid": cid, "format": "md"})
    assert isinstance(md, str) and "Plans" in md
    assert await c.call("chat.stop", {"cid": cid}) == {"stopped": False}
    assert await c.call("chat.conversation.delete", {"cid": cid}) == {"ok": True}
    with pytest.raises(sc.SnpError) as e:
        await c.call("chat.conversation.get", {"cid": cid})
    assert e.value.code == "not_found" and e.value.status == 404 and e.value.retryable is False


@pytest.mark.asyncio
async def test_settings_models_and_system(snp):
    c = await snp.client()
    s = await c.call("settings.get")
    assert "worker_idle_unload_minutes" in s
    p = await c.call("settings.patch", {"patch": {"worker_idle_unload_minutes": 3}})
    assert p["worker_idle_unload_minutes"] == 3
    with pytest.raises(sc.SnpError) as e:
        await c.call("settings.patch", {"patch": {"bogus": 1}})
    assert e.value.code == "bad_request" and e.value.status == 400
    assert isinstance(await c.call("model.list"), list)
    assert isinstance(await c.call("model.list", {"category": "llm"}), list)
    assert {m["id"] for m in await c.call("model.catalog")} >= {"sdxl-turbo"}
    assert isinstance(await c.call("model.presets"), (list, dict))
    assert isinstance(await c.call("model.disk"), dict)
    h = await c.call("system.health")
    assert h["status"] == "ok" and h["service"] == "sarness-engine"
    info = await c.call("system.info")
    assert isinstance(info, dict) and info
    assert await c.call("job.list") == []
    assert await c.call("job.list", {"status": "queued", "limit": 5}) == []
    assert isinstance(await c.call("download.list"), list)
    assert isinstance(await c.call("runtime.list"), (list, dict))
    assert isinstance(await c.call("llm.status"), dict)
    assert isinstance(await c.call("gateway.status"), dict)
    assert isinstance(await c.call("agent.models"), list)
    assert isinstance(await c.call("workflow.nodes"), (list, dict))
    assert isinstance(await c.call("tag.list"), list)
    assert isinstance(await c.call("library.models"), list)


@pytest.mark.asyncio
async def test_projects_folders_collections_generations(snp):
    c = await snp.client()
    p = await c.call("project.create", {"body": {"name": "Moodboards"}})
    f = await c.call("folder.create", {"body": {"project_id": p["id"], "name": "Sci-fi"}})
    listed = await c.call("project.list")
    assert listed[0]["folders"][0]["id"] == f["id"]
    assert (await c.call("project.update", {"pid": p["id"], "body": {"name": "Boards"}}))["name"] == "Boards"
    assert (await c.call("folder.update", {"fid": f["id"], "body": {"name": "SF"}}))["name"] == "SF"
    col = await c.call("collection.create", {"body": {"name": "Faves"}})
    assert (await c.call("collection.rename", {"cid": col["id"], "body": {"name": "Best"}}))["name"] == "Best"
    assert await c.call("collection.add_items", {"cid": col["id"], "body": {"generation_ids": []}}) == {"ok": True}
    assert [x["name"] for x in await c.call("collection.list")] == ["Best"]
    assert await c.call("collection.remove_item", {"cid": col["id"], "gid": "g_x"}) == {"ok": True}
    assert await c.call("collection.delete", {"cid": col["id"]}) == {"ok": True}
    gens = await c.call("generation.list", {"limit": 10, "favorite": False})
    assert gens == {"items": [], "next_cursor": None}
    with pytest.raises(sc.SnpError) as e:
        await c.call("generation.get", {"gid": "g_none"})
    assert e.value.code == "not_found"
    assert (await c.call("generation.bulk", {"body": {"ids": []}}))["count"] == 0
    with pytest.raises(sc.SnpError) as e:
        await c.call("project.create", {"body": {}})
    assert e.value.code == "bad_request" and "name is required" in e.value.info["message"]
    assert await c.call("folder.delete", {"fid": f["id"]}) == {"ok": True}
    assert await c.call("project.delete", {"pid": p["id"]}) == {"ok": True}


@pytest.mark.asyncio
async def test_experiments_and_paintbrush(snp):
    c = await snp.client()
    exps = await c.call("experiment.list")
    assert any(e["id"] == "gods-paintbrush" for e in exps)
    doc = await c.call("experiment.document.create", {"eid": "gods-paintbrush", "body": {"title": "T", "data": {"a": 1}}})
    got = await c.call("experiment.document.get", {"eid": "gods-paintbrush", "doc_id": doc["id"]})
    assert got["data"] == {"a": 1}
    assert len(await c.call("experiment.document.list", {"eid": "gods-paintbrush"})) == 1
    assert await c.call("experiment.document.delete", {"eid": "gods-paintbrush", "doc_id": doc["id"]}) == {"ok": True}
    with pytest.raises(sc.SnpError) as e:
        await c.call("experiment.get", {"eid": "nope"})
    assert e.value.code == "not_found"
    st = await c.call("paintbrush.status")
    assert isinstance(st, dict)


@pytest.mark.asyncio
async def test_job_and_workflow_validation_errors_carry_status(snp):
    c = await snp.client()
    with pytest.raises(sc.SnpError) as e:
        await c.call("job.submit", {"body": {"kind": "image", "workflow": {"nodes": [], "edges": []}}})
    assert e.value.code == "bad_request" and e.value.status in (400, 422)
    with pytest.raises(sc.SnpError) as e:
        await c.call("job.get", {"job_id": "j_none"})
    assert e.value.code == "not_found"
    with pytest.raises(sc.SnpError) as e:
        await c.call("model.delete", {"model_id": "local/nothing"})
    assert e.value.code == "not_found"
    with pytest.raises(sc.SnpError) as e:
        await c.call("download.action", {"did": "d_none", "action": "pause"})
    assert e.value.code == "not_found"


@pytest.mark.asyncio
async def test_results_match_the_http_api(snp):
    """The same route functions answer both protocols: results are identical."""
    c = await snp.client()
    p = await c.call("project.create", {"body": {"name": "Parity"}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=snp.app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {snp.token}"}) as h:
        for op, path in (("project.list", "/v1/projects"), ("settings.get", "/v1/settings"),
                         ("model.list", "/v1/models"), ("job.list", "/v1/jobs"), ("tag.list", "/v1/tags"),
                         ("chat.conversation.list", "/v1/chat/conversations"), ("collection.list", "/v1/collections"),
                         ("experiment.list", "/v1/experiments"), ("download.list", "/v1/downloads")):
            r = await h.get(path)
            assert r.status_code == 200, path
            assert json.loads(json.dumps(await c.call(op))) == r.json(), op
        # HTTP still answers normally while SNP is up.
        assert (await h.get("/v1/health")).json()["status"] == "ok"
        assert (await h.get("/v1/projects", headers={"Authorization": "Bearer no"})).status_code == 401
    assert p["name"] == "Parity"


@pytest.mark.asyncio
async def test_file_sign_returns_signed_url(snp):
    c = await snp.client()
    up = await _upload(c, _png())
    signed = await c.call("file.sign", {"fid": up["id"]})
    assert signed["url"].startswith(f"/v1/files/{up['id']}?exp=")
    with pytest.raises(sc.SnpError) as e:
        await c.call("file.sign", {"fid": "f_none"})
    assert e.value.code == "not_found"


def _png(size=8) -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), "white").save(buf, "PNG")
    return buf.getvalue()


async def _upload(c, data: bytes, **args):
    s = await c.open("file.upload", {"name": "a.png", **args})
    for i in range(0, len(data), 60000):
        await s.send_data(data[i:i + 60000])
    s.end()
    return await s.result()


# ----------------------------------------------------------------------------- unauthenticated access
@pytest.mark.asyncio
async def test_no_operation_without_authentication(snp):
    for op in ("settings.get", "system.health", "event.subscribe", "file.get", "file.upload"):
        r = await snp.raw()
        await r.hello()
        r.send(sc.OPEN, 1, sc.cenc({"op": op, "args": {}}))
        frames = []
        while True:
            f = await r.recv(5)
            if f is None:
                break
            frames.append(f)
        assert [f[0] for f in frames] == [sc.GOAWAY], (op, frames)
        assert sc.cdec(frames[0][2])["code"] == "protocol_error"
