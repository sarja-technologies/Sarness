"""Workflow executor: runs a validated graph node-by-node in topological order.

Features: per-node result caching keyed by (type, params, upstream keys),
cancellation checks between and inside nodes, error attribution to the failing
node with downstream nodes marked as skipped, weighted progress reporting and
lineage metadata propagated from sources to outputs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import registry
from .graph import order
from .types import Cancelled, NodeSpec, WorkflowError

log = logging.getLogger("sarness.executor")

Emit = Callable[[dict[str, Any]], None]


class NodeCache:
    """LRU cache of node outputs. Cleared on OOM and when models are unloaded."""

    def __init__(self, capacity: int = 96):
        self.capacity = capacity
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        v = self._data.get(key)
        if v is not None:
            self._data.move_to_end(key)
        return v

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class NodeContext:
    """Handed to every node's ``run`` function."""

    job_id: str
    node_id: str
    services: Any                     # runtime services (model manager, paths, …)
    emit: Emit
    cancel_event: threading.Event
    lineage: dict[str, Any] = field(default_factory=dict)
    _annotations: dict[str, Any] = field(default_factory=dict)
    _weight_done: float = 0.0
    _weight_total: float = 1.0
    _weight_self: float = 1.0
    _last_emit: float = 0.0

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def annotate(self, **values: Any) -> None:
        """Record provenance metadata that flows downstream to outputs."""
        self._annotations.update({k: v for k, v in values.items() if v is not None})

    def progress(self, step: int, total: int, text: str | None = None, preview: str | None = None) -> None:
        self.check_cancel()
        now = time.monotonic()
        frac = (step / total) if total else 0.0
        if preview is None and step < total and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        overall = (self._weight_done + self._weight_self * frac) / max(self._weight_total, 1e-6)
        ev = {"type": "progress", "job_id": self.job_id, "node_id": self.node_id,
              "step": step, "total": total, "progress": round(min(overall, 0.999), 4), "text": text}
        self.emit(ev)
        if preview:
            self.emit({"type": "preview", "job_id": self.job_id, "node_id": self.node_id, "image": preview})


def _stable(v: Any) -> Any:
    try:
        return json.loads(json.dumps(v, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return str(v)


def execute(graph: dict[str, Any], *, job_id: str, services: Any, emit: Emit,
            cancel_event: threading.Event, cache: NodeCache | None = None) -> dict[str, Any]:
    """Run ``graph``. Returns ``{"outputs": [...], "node_states": {...}}``.

    Raises :class:`WorkflowError` (with ``node_id``) or :class:`Cancelled`.
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    plan = order(graph)
    incoming: dict[str, dict[str, tuple[str, str]]] = {}
    for e in graph.get("edges", []):
        incoming.setdefault(e["target"], {})[e["target_input"]] = (e["source"], e["source_output"])

    specs: dict[str, NodeSpec] = {}
    for nid in plan:
        spec = registry.get(nodes[nid]["type"])
        if spec is None:
            raise WorkflowError(f"unknown node type {nodes[nid]['type']}", nid)
        specs[nid] = spec

    weights = {nid: (10.0 if specs[nid].gpu else 1.0) for nid in plan}
    total_w = sum(weights.values()) or 1.0
    done_w = 0.0

    results: dict[str, dict[str, Any]] = {}
    keys: dict[str, str] = {}
    lineage: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {nid: {"status": "pending"} for nid in plan}
    outputs: list[dict[str, Any]] = []

    def set_state(nid: str, status: str, **extra: Any) -> None:
        states[nid] = {"status": status, **extra}
        emit({"type": "node", "job_id": job_id, "node_id": nid, "status": status, **extra})

    for idx, nid in enumerate(plan):
        if cancel_event.is_set():
            raise Cancelled()
        node = nodes[nid]
        spec = specs[nid]
        params = {p.name: (node.get("params") or {}).get(p.name, p.default) for p in spec.params}

        # Gather inputs; socket-capable params can be overridden by edges.
        inputs: dict[str, Any] = {}
        upstream_keys: list[str] = []
        merged_lineage: dict[str, Any] = {}
        for name, (src, out) in sorted(incoming.get(nid, {}).items()):
            if src not in results:
                continue  # pruned branch
            src_spec = specs[src]
            src_out = results[src]
            value = next(iter(src_out.values()), None) if src_spec.passthrough else src_out.get(out)
            if spec.param(name) is not None and spec.input(name) is None:
                params[name] = value
            else:
                inputs[name] = value
            upstream_keys.append(f"{name}={keys[src]}:{out}")
            for k, v in lineage.get(src, {}).items():
                merged_lineage.setdefault(k, v)

        key_src = json.dumps({"t": spec.type, "p": _stable(params), "u": upstream_keys}, sort_keys=True)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:24]
        keys[nid] = key

        ctx = NodeContext(job_id=job_id, node_id=nid, services=services, emit=emit,
                          cancel_event=cancel_event, lineage=merged_lineage,
                          _weight_done=done_w, _weight_total=total_w, _weight_self=weights[nid])

        cached = cache.get(key) if (cache is not None and spec.cacheable and not spec.output_node) else None
        if cached is not None:
            results[nid] = cached["out"]
            lineage[nid] = {**merged_lineage, **cached["ann"]}
            set_state(nid, "cached")
            done_w += weights[nid]
            continue

        set_state(nid, "running")
        t0 = time.perf_counter()
        try:
            out = spec.run(ctx, inputs, params) or {}
        except (Cancelled, WorkflowError):
            _mark_skipped(plan[idx + 1:], states, set_state)
            raise
        except Exception as exc:  # noqa: BLE001 — attribute every failure to its node
            oom = _is_oom(exc)
            log.error("node %s (%s) failed: %s\n%s", nid, spec.type, exc, traceback.format_exc())
            set_state(nid, "error", message=_friendly(exc))
            _mark_skipped(plan[idx + 1:], states, set_state)
            raise WorkflowError(_friendly(exc), nid, oom=oom) from exc
        elapsed = round(time.perf_counter() - t0, 3)
        results[nid] = out
        lineage[nid] = {**merged_lineage, **ctx._annotations}
        if cache is not None and spec.cacheable and not spec.output_node:
            cache.put(key, {"out": out, "ann": ctx._annotations})
        if spec.output_node:
            for item in out.get("_outputs", []):
                outputs.append({**item, "node_id": nid, "lineage": lineage[nid]})
        set_state(nid, "done", elapsed_s=elapsed)
        done_w += weights[nid]

    return {"outputs": outputs, "node_states": states}


def _mark_skipped(rest: list[str], states: dict, set_state: Callable) -> None:
    for nid in rest:
        if states.get(nid, {}).get("status") == "pending":
            set_state(nid, "skipped")


def _is_oom(exc: BaseException) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    return name == "OutOfMemoryError" or "out of memory" in msg or "cuda error: out of memory" in msg


def _friendly(exc: BaseException) -> str:
    if _is_oom(exc):
        from ..runtime.models import unified_memory

        if unified_memory():
            return ("The GPU ran out of memory. Try a smaller resolution, fewer frames or a smaller batch, "
                    "or unload other models.")
        return ("The GPU ran out of memory. Try a smaller resolution, fewer frames, a smaller batch, "
                "or switch the memory mode to 'Offload'.")
    msg = str(exc).strip() or type(exc).__name__
    return msg if len(msg) < 600 else msg[:600] + "…"
