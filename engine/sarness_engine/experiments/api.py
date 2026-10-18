"""``/v1/experiments`` — the experiment catalogue and saved experiment documents."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..api.deps import AppState, require_auth
from .documents import DocumentError

router = APIRouter(prefix="/v1/experiments")


def _exp(st: AppState, eid: str):
    exp = st.experiments.get(eid) if st.experiments else None
    if exp is None:
        raise HTTPException(404, f"unknown experiment {eid!r}")
    return exp


def _docs(fn, *args):
    try:
        return fn(*args)
    except DocumentError as exc:
        raise HTTPException(exc.status, str(exc)) from None


def _changed(st: AppState, eid: str, doc_id: str, action: str) -> None:
    st.bus.publish("experiment.documents", {"experiment": eid, "document_id": doc_id, "action": action})


@router.get("")
def list_experiments(st: AppState = Depends(require_auth)) -> list[dict[str, Any]]:
    return [e.summary() for e in st.experiments.list()]


@router.get("/{eid}")
async def get_experiment(eid: str, st: AppState = Depends(require_auth)) -> dict[str, Any]:
    return await asyncio.to_thread(_exp(st, eid).status)


@router.get("/{eid}/documents")
def list_documents(eid: str, st: AppState = Depends(require_auth)):
    _exp(st, eid)
    return st.experiment_docs.list(eid)


@router.post("/{eid}/documents")
def create_document(eid: str, body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    _exp(st, eid)
    doc = _docs(st.experiment_docs.create, eid, body.get("title") or "Untitled", body.get("data"),
                body.get("thumbnail"), body.get("project_id"))
    _changed(st, eid, doc["id"], "created")
    return doc


@router.get("/{eid}/documents/{doc_id}")
def get_document(eid: str, doc_id: str, st: AppState = Depends(require_auth)):
    _exp(st, eid)
    return _docs(st.experiment_docs.get, eid, doc_id)


@router.put("/{eid}/documents/{doc_id}")
def update_document(eid: str, doc_id: str, body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    _exp(st, eid)
    doc = _docs(st.experiment_docs.update, eid, doc_id, body)
    _changed(st, eid, doc_id, "updated")
    return doc


@router.post("/{eid}/documents/{doc_id}/duplicate")
def duplicate_document(eid: str, doc_id: str, body: dict[str, Any] = Body(default={}),
                       st: AppState = Depends(require_auth)):
    _exp(st, eid)
    doc = _docs(st.experiment_docs.duplicate, eid, doc_id, body.get("title"))
    _changed(st, eid, doc["id"], "created")
    return doc


@router.delete("/{eid}/documents/{doc_id}")
def delete_document(eid: str, doc_id: str, st: AppState = Depends(require_auth)):
    _exp(st, eid)
    _docs(st.experiment_docs.delete, eid, doc_id)
    _changed(st, eid, doc_id, "deleted")
    return {"ok": True}
