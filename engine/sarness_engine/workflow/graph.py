"""Graph validation and topological planning for Sarness workflows."""

from __future__ import annotations

import copy
import random
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from . import registry
from .types import ANY, WORKFLOW_FORMAT, WORKFLOW_VERSION, NodeSpec, compatible

ModelLookup = Callable[[str], "dict[str, Any] | None"]


@dataclass
class Issue:
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    level: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "node_id": self.node_id, "edge_id": self.edge_id, "level": self.level}


def empty_workflow(kind: str = "image", name: str = "Untitled workflow") -> dict[str, Any]:
    return {"format": WORKFLOW_FORMAT, "version": WORKFLOW_VERSION, "name": name, "kind": kind,
            "nodes": [], "edges": [], "groups": []}


def _input_type(spec: NodeSpec, name: str) -> str | None:
    s = spec.input(name)
    if s:
        return s.type
    p = spec.param(name)
    if p and p.socket:
        return p.socket_type
    return None


def resolve_output_type(graph: dict[str, Any], node_id: str, output: str, _seen: set | None = None) -> str | None:
    """Resolve an output's concrete type, following passthrough (reroute) nodes upstream."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    n = nodes.get(node_id)
    if not n:
        return None
    spec = registry.get(n["type"])
    if not spec:
        return None
    if spec.passthrough:
        _seen = _seen or set()
        if node_id in _seen:
            return ANY
        _seen.add(node_id)
        for e in graph.get("edges", []):
            if e["target"] == node_id:
                return resolve_output_type(graph, e["source"], e["source_output"], _seen)
        return ANY
    s = spec.output(output)
    return s.type if s else None


def validate(graph: dict[str, Any], model_lookup: ModelLookup | None = None) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(graph, dict):
        return [Issue("workflow must be an object")]
    if graph.get("format", WORKFLOW_FORMAT) != WORKFLOW_FORMAT:
        issues.append(Issue(f"unknown workflow format {graph.get('format')!r}"))
    if int(graph.get("version", WORKFLOW_VERSION)) > WORKFLOW_VERSION:
        issues.append(Issue(f"workflow version {graph.get('version')} is newer than this engine supports"))

    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id: dict[str, dict[str, Any]] = {}
    for n in nodes:
        nid = n.get("id")
        if not nid or nid in by_id:
            issues.append(Issue("duplicate or missing node id", nid))
            continue
        by_id[nid] = n
        spec = registry.get(n.get("type", ""))
        if spec is None:
            issues.append(Issue(f"unknown node type '{n.get('type')}'", nid))
            continue
        params = n.get("params") or {}
        connected = {e.get("target_input") for e in edges if e.get("target") == nid}
        for p in spec.params:
            if p.name in connected:
                continue
            err = p.validate(params.get(p.name, p.default))
            if err:
                issues.append(Issue(err, nid))
            if p.kind == "model" and model_lookup and params.get(p.name):
                info = model_lookup(params[p.name])
                if info is None:
                    issues.append(Issue(f"model '{params[p.name]}' is not installed", nid))
                elif p.model_category and info.get("category") != p.model_category:
                    issues.append(Issue(f"'{info.get('name')}' is a {info.get('category')} model, expected {p.model_category}", nid))
                elif p.model_family and info.get("family") not in p.model_family:
                    issues.append(Issue(f"'{info.get('name')}' ({info.get('family')}) is not supported by this node", nid))

    seen_inputs: set[tuple[str, str]] = set()
    for e in edges:
        eid = e.get("id")
        s, t = by_id.get(e.get("source")), by_id.get(e.get("target"))
        if not s or not t:
            issues.append(Issue("connection references a missing node", edge_id=eid))
            continue
        sspec, tspec = registry.get(s["type"]), registry.get(t["type"])
        if not sspec or not tspec:
            continue
        if not sspec.passthrough and not sspec.output(e.get("source_output", "")):
            issues.append(Issue(f"'{sspec.title}' has no output '{e.get('source_output')}'", s["id"], eid))
            continue
        key = (t["id"], e.get("target_input", ""))
        if key in seen_inputs:
            issues.append(Issue(f"input '{key[1]}' has more than one connection", t["id"], eid))
        seen_inputs.add(key)
        dst_type = ANY if tspec.passthrough else _input_type(tspec, e.get("target_input", ""))
        if dst_type is None:
            issues.append(Issue(f"'{tspec.title}' has no input '{e.get('target_input')}'", t["id"], eid))
            continue
        src_type = resolve_output_type(graph, s["id"], e.get("source_output", ""))
        if src_type and not compatible(src_type, dst_type):
            issues.append(Issue(f"cannot connect {src_type} to {dst_type} ('{e.get('target_input')}')", t["id"], eid))

    for nid, n in by_id.items():
        spec = registry.get(n["type"])
        if not spec:
            continue
        for sock in spec.inputs:
            if not sock.optional and (nid, sock.name) not in seen_inputs:
                issues.append(Issue(f"required input '{sock.label or sock.name}' is not connected", nid))

    if model_lookup:
        issues += _video_frame_issues(by_id, edges, model_lookup)

    try:
        order(graph)
    except ValueError as exc:
        issues.append(Issue(str(exc)))

    if not any(registry.get(n["type"]) and registry.get(n["type"]).output_node for n in by_id.values()):
        issues.append(Issue("workflow has no output node (add Save Image or Save Video)"))
    return issues


def _video_frame_issues(by_id: dict[str, dict[str, Any]], edges: list[dict[str, Any]],
                        model_lookup: ModelLookup) -> list[Issue]:
    """Start and end frames against what the loaded video model accepts, so a job is refused before
    anything loads (the sampler checks the same again)."""
    issues: list[Issue] = []
    into = {(e.get("target"), e.get("target_input")): e.get("source") for e in edges}
    for nid, n in by_id.items():
        if n.get("type") != "sampler.video":
            continue
        loader = by_id.get(into.get((nid, "model")))
        model = loader and (loader.get("params") or {}).get("model")
        info = model_lookup(model) if model else None
        if not info:
            continue
        caps = info.get("capabilities") or {}
        modes = caps.get("modes") or ["t2v"]
        has_start = (nid, "start_image") in into
        if has_start and "i2v" not in modes:
            issues.append(Issue(f"{info.get('name')} does not support image-to-video", nid))
        if not has_start and "t2v" not in modes:
            issues.append(Issue(f"{info.get('name')} needs a start frame (image-to-video only)", nid))
        if (nid, "end_image") in into and not caps.get("end_frame"):
            issues.append(Issue(f"{info.get('name')} does not support end frames", nid))
    return issues


def order(graph: dict[str, Any], *, prune: bool = True) -> list[str]:
    """Kahn topological order. With ``prune`` only ancestors of output nodes are kept."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    deps: dict[str, set[str]] = defaultdict(set)
    rdeps: dict[str, set[str]] = defaultdict(set)
    for e in graph.get("edges", []):
        if e["source"] in nodes and e["target"] in nodes:
            deps[e["target"]].add(e["source"])
            rdeps[e["source"]].add(e["target"])

    keep = set(nodes)
    if prune:
        outs = [nid for nid, n in nodes.items() if (sp := registry.get(n["type"])) and sp.output_node]
        keep = set()
        stack = list(outs)
        while stack:
            nid = stack.pop()
            if nid in keep:
                continue
            keep.add(nid)
            stack.extend(deps[nid])

    indeg = {nid: len(deps[nid] & keep) for nid in keep}
    # Stable order: follow original node order for ties.
    position = {n["id"]: i for i, n in enumerate(graph.get("nodes", []))}
    ready = deque(sorted((nid for nid, d in indeg.items() if d == 0), key=position.get))
    result: list[str] = []
    while ready:
        nid = ready.popleft()
        result.append(nid)
        for nxt in sorted(rdeps[nid] & keep, key=position.get):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(result) != len(keep):
        raise ValueError("workflow contains a cycle")
    return result


# The remote AI gateway validates seeds as signed 32-bit integers (HTTP 422 above this).
SEED_MAX = 2**31 - 1


def clamp_seed(value: int) -> int:
    """Map any non-negative seed into the gateway-safe range without changing in-range values."""
    return int(value) % (SEED_MAX + 1)


def resolve_seeds(graph: dict[str, Any], rng: random.Random | None = None) -> dict[str, Any]:
    """Return a copy of the graph with every negative seed replaced by a concrete value."""
    rng = rng or random.SystemRandom()
    g = copy.deepcopy(graph)
    for n in g.get("nodes", []):
        spec = registry.get(n.get("type", ""))
        if not spec:
            continue
        params = n.setdefault("params", {})
        for p in spec.params:
            if p.kind == "seed":
                v = params.get(p.name, p.default)
                if v is None or v < 0:
                    params[p.name] = rng.randrange(0, SEED_MAX)
    return g


def referenced_models(graph: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for n in graph.get("nodes", []):
        spec = registry.get(n.get("type", ""))
        if not spec:
            continue
        for p in spec.params:
            if p.kind == "model":
                v = (n.get("params") or {}).get(p.name)
                if v and v not in out:
                    out.append(v)
    return out


def referenced_files(graph: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for n in graph.get("nodes", []):
        spec = registry.get(n.get("type", ""))
        if not spec:
            continue
        for p in spec.params:
            if p.kind == "file":
                v = (n.get("params") or {}).get(p.name)
                if v and v not in out:
                    out.append(v)
    return out
