"""Chat conversations and streaming generation (Server-Sent Events)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..chat.service import ChatError
from .deps import AppState, require_auth

router = APIRouter(prefix="/v1/chat")


@router.get("/conversations")
def list_conversations(q: str | None = None, project_id: str | None = None, st: AppState = Depends(require_auth)):
    return st.chat.list(q=q, project_id=project_id)


@router.post("/conversations")
def create_conversation(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return st.chat.create(title=body.get("title"), model_ref=body.get("model_ref"),
                          system_prompt=body.get("system_prompt"), project_id=body.get("project_id"),
                          folder_id=body.get("folder_id"), params=body.get("params"))


@router.get("/conversations/{cid}")
def get_conversation(cid: str, st: AppState = Depends(require_auth)):
    return st.chat.get(cid)


@router.patch("/conversations/{cid}")
def update_conversation(cid: str, body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return st.chat.update(cid, body)


@router.delete("/conversations/{cid}")
def delete_conversation(cid: str, st: AppState = Depends(require_auth)):
    st.chat.delete(cid)
    return {"ok": True}


@router.delete("/conversations/{cid}/messages/{mid}")
def delete_branch(cid: str, mid: str, st: AppState = Depends(require_auth)):
    rows = st.db.all("SELECT id, parent_id FROM messages WHERE conversation_id = ?", [cid])
    if not any(r["id"] == mid for r in rows):
        raise HTTPException(404, "message not found")
    doomed = {mid}
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r["parent_id"] in doomed and r["id"] not in doomed:
                doomed.add(r["id"])
                changed = True
    marks = ",".join("?" * len(doomed))
    st.db.execute(f"DELETE FROM messages WHERE id IN ({marks})", list(doomed))
    conv = st.db.one("SELECT active_leaf_id FROM conversations WHERE id = ?", [cid])
    if conv and conv["active_leaf_id"] in doomed:
        parent = next((r["parent_id"] for r in rows if r["id"] == mid), None)
        st.db.update("conversations", cid, {"active_leaf_id": parent})
    return st.chat.get(cid)


@router.post("/conversations/{cid}/generate")
async def generate(cid: str, body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    # Validate synchronously so errors come back as normal HTTP errors.
    gen = st.chat.generate(cid, body)
    try:
        first = await gen.__anext__()
    except ChatError as exc:
        raise HTTPException(exc.status, str(exc))
    except StopAsyncIteration:
        raise HTTPException(500, "no response")

    async def stream():
        yield _sse(first)
        try:
            async for ev in gen:
                yield _sse(ev)
        except ChatError as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(ev: dict[str, Any]) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@router.post("/conversations/{cid}/stop")
def stop(cid: str, st: AppState = Depends(require_auth)):
    return {"stopped": st.chat.stop(cid)}


@router.get("/conversations/{cid}/export")
def export(cid: str, format: str = "json", st: AppState = Depends(require_auth)):
    if format == "md":
        return PlainTextResponse(st.chat.export_markdown(cid), media_type="text/markdown")
    return st.chat.export(cid)


@router.post("/import")
def import_conversation(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return st.chat.import_(body)


agent_router = APIRouter(prefix="/v1/agent")


@agent_router.get("/models")
def agent_models(st: AppState = Depends(require_auth)):
    return st.chat.agent_models()


@agent_router.post("/completions")
async def agent_completions(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    """Stateless streaming completion with native tool calls (OpenAI-style).

    The engine only runs inference. The calling desktop owns the transcript and executes tools."""
    gen = st.chat.agent_completion(body)
    try:
        first = await gen.__anext__()
    except ChatError as exc:
        raise HTTPException(exc.status, str(exc))
    except StopAsyncIteration:
        raise HTTPException(500, "no response")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"model server error: {exc}")

    async def stream():
        yield _sse(first)
        try:
            async for ev in gen:
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
