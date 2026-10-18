"""Experiment registry.

An *experiment* is a self-contained tool living next to the main Chat / Code /
Image / Video workspace. Each one provides metadata (id, name, tagline,
capabilities), an availability check, optional HTTP routes under
``/v1/experiments/<id>/…`` and, if it uses the GPU, lifecycle hooks so the job
scheduler can take the card back when a queued image or video job needs it.

Experiments are discovered from :data:`sarness_engine.experiments.BUILTIN`;
adding one means adding a package with a ``create(state)`` factory and a
``router`` and listing it there.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("sarness.experiments")


class Experiment:
    id: str = ""
    name: str = ""
    tagline: str = ""
    description: str = ""
    version: str = "1"
    capabilities: tuple[str, ...] = ()

    def availability(self) -> dict[str, Any]:
        """``{"state": "ready" | "partial" | "needs_install" | "unavailable", "reasons": [...]}``"""
        return {"state": "ready", "reasons": []}

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "tagline": self.tagline, "description": self.description,
                "version": self.version, "capabilities": list(self.capabilities),
                "availability": self.availability(), "gpu_active": self.gpu_active()}

    def status(self) -> dict[str, Any]:
        return self.summary()

    async def startup(self) -> None:  # noqa: B027 - optional hook
        pass

    async def shutdown(self) -> None:  # noqa: B027
        pass

    def gpu_active(self) -> bool:
        """True while the experiment holds GPU memory or is computing."""
        return False

    async def yield_gpu(self, reason: str) -> None:  # noqa: B027
        """Called before Sarness starts a GPU job: finish or cancel work and free VRAM."""


class ExperimentRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Experiment] = {}

    def register(self, exp: Experiment) -> None:
        if not exp.id or exp.id in self._items:
            raise ValueError(f"invalid or duplicate experiment id {exp.id!r}")
        self._items[exp.id] = exp

    def get(self, eid: str) -> Experiment | None:
        return self._items.get(eid)

    def list(self) -> list[Experiment]:
        return list(self._items.values())

    async def startup(self) -> None:
        for e in self._items.values():
            try:
                await e.startup()
            except Exception:  # an experiment must never stop the engine from starting
                log.exception("experiment %s failed to start", e.id)

    async def shutdown(self) -> None:
        for e in self._items.values():
            try:
                await e.shutdown()
            except Exception:
                log.exception("experiment %s failed to shut down", e.id)

    def gpu_active(self) -> bool:
        return any(e.gpu_active() for e in self._items.values())

    async def yield_gpu(self, reason: str) -> None:
        for e in self._items.values():
            if e.gpu_active():
                try:
                    await e.yield_gpu(reason)
                except Exception:
                    log.exception("experiment %s did not release the GPU cleanly", e.id)
