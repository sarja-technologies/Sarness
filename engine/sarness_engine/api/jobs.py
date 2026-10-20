"""Jobs, node library and saved workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..db import new_id, now
from ..workflow import graph as wf
from ..workflow import registry, templates
from ..workflow.types import SOCKET_COLORS, SOCKET_TYPES, WORKFLOW_FORMAT, WORKFLOW_VERSION
from .deps import AppState, require_auth

router = APIRouter(prefix="/v1")


@router.post("/jobs")
def submit_job(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    kind = body.get("kind")
    if kind not in ("image", "video"):
        raise HTTPException(400, "kind must be 'image' or 'video'")
    return st.jobs.submit(kind=kind, simple=body.get("simple"), workflow=body.get("workflow"),
                          project_id=body.get("project_id"), folder_id=body.get("folder_id"), title=body.get("title"),
                          client_key=body.get("client_key"))


@router.get("/jobs")
def list_jobs(status: str | None = None, kind: str | None = None, limit: int = 100,
              st: AppState = Depends(require_auth)):
    return st.jobs.list(status=status, kind=kind, limit=min(limit, 500))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, st: AppState = Depends(require_auth)):
    job = st.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    gens = st.db.all("SELECT * FROM generations WHERE job_id = ? ORDER BY created_at", [job_id])
    job["generations"] = [st.jobs.generation_public(g) for g in gens]
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, st: AppState = Depends(require_auth)):
    return st.jobs.cancel(job_id)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return st.jobs.retry(job_id, new_seed=bool(body.get("new_seed")))


@router.delete("/jobs/finished")
def clear_finished(st: AppState = Depends(require_auth)):
    return {"deleted": st.jobs.clear_finished()}


# ---------------------------------------------------------------- node library
@router.get("/nodes")
def nodes(st: AppState = Depends(require_auth)):
    return {
        "format": WORKFLOW_FORMAT, "version": WORKFLOW_VERSION,
        "socket_types": [{"type": t, "color": SOCKET_COLORS.get(t, "#d4d4d8")} for t in SOCKET_TYPES],
        "categories": registry.CATEGORY_ORDER,
        "nodes": [s.to_dict() for s in registry.all_specs()],
    }


@router.post("/workflows/validate")
def validate(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    issues = wf.validate(body.get("workflow") or {}, st.jobs.model_lookup)
    return {"valid": not any(i.level == "error" for i in issues), "issues": [i.to_dict() for i in issues]}


@router.post("/workflows/compile")
def compile_simple(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    kind = body.get("kind", "image")
    simple = body.get("simple") or {}
    return templates.image_workflow(simple) if kind == "image" else templates.video_workflow(simple)


@router.post("/workflows/simplify")
def simplify(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return templates.to_simple(body.get("workflow") or {})


@router.get("/workflows")
def list_workflows(kind: str | None = None, presets: bool | None = None, st: AppState = Depends(require_auth)):
    conds, args = [], []
    if kind:
        conds.append("kind = ?")
        args.append(kind)
    if presets is not None:
        conds.append("is_preset = ?")
        args.append(int(presets))
    sql = "SELECT id, name, kind, description, is_preset, project_id, created_at, updated_at FROM workflows"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return st.db.all(sql + " ORDER BY updated_at DESC", args)


def _check_graph(graph: dict[str, Any]) -> None:
    if not isinstance(graph, dict) or graph.get("format", WORKFLOW_FORMAT) != WORKFLOW_FORMAT:
        raise HTTPException(400, "not a Sarness workflow")
    if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise HTTPException(400, "workflow must contain 'nodes' and 'edges' arrays")


@router.post("/workflows")
def save_workflow(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    graph = body.get("graph") or {}
    _check_graph(graph)
    row = {"id": new_id("w_"), "name": (body.get("name") or graph.get("name") or "Workflow")[:120],
           "kind": body.get("kind") or graph.get("kind") or "image", "description": body.get("description") or "",
           "graph": graph, "is_preset": int(bool(body.get("is_preset"))), "project_id": body.get("project_id"),
           "created_at": now(), "updated_at": now()}
    st.db.insert("workflows", row)
    return row


@router.get("/workflows/{wid}")
def get_workflow(wid: str, st: AppState = Depends(require_auth)):
    row = st.db.one("SELECT * FROM workflows WHERE id = ?", [wid])
    if not row:
        raise HTTPException(404, "workflow not found")
    return row


@router.put("/workflows/{wid}")
def update_workflow(wid: str, body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    get_workflow(wid, st)
    vals = {k: v for k, v in body.items() if k in ("name", "description", "graph", "is_preset", "kind")}
    if "graph" in vals:
        _check_graph(vals["graph"])
    if "is_preset" in vals:
        vals["is_preset"] = int(bool(vals["is_preset"]))
    vals["updated_at"] = now()
    st.db.update("workflows", wid, vals)
    return get_workflow(wid, st)


@router.delete("/workflows/{wid}")
def delete_workflow(wid: str, st: AppState = Depends(require_auth)):
    st.db.execute("DELETE FROM workflows WHERE id = ?", [wid])
    return {"ok": True}
