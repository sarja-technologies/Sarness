"""Experiments: self-contained AI tools next to the main workspace.

To add an experiment, create a package exposing ``create(state) -> Experiment``
and an APIRouter named ``router`` (under ``/v1/experiments/<id>``), then list
the package in :data:`BUILTIN`. Experiments must not import torch at module
level; GPU code belongs in an :class:`~.worker.ExperimentWorker` handler.
"""

from __future__ import annotations

import importlib
import logging

from .registry import Experiment, ExperimentRegistry

log = logging.getLogger("sarness.experiments")

BUILTIN: tuple[str, ...] = (
    "sarness_engine.experiments.paintbrush",
)


def build_registry(st) -> ExperimentRegistry:
    reg = ExperimentRegistry()
    for name in BUILTIN:
        try:
            reg.register(importlib.import_module(name).create(st))
        except Exception:  # a broken experiment must not take the engine down
            log.exception("could not load experiment %s", name)
    return reg


def routers() -> list:
    from .api import router as catalogue

    out = [catalogue]
    for name in BUILTIN:
        try:
            out.append(importlib.import_module(name).router)
        except Exception:
            log.exception("could not load routes for experiment %s", name)
    return out


__all__ = ["BUILTIN", "Experiment", "ExperimentRegistry", "build_registry", "routers"]
