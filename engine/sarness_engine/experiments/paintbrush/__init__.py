"""God's Paintbrush — semantic painting to photorealistic landscapes (an NVIDIA Canvas-style experiment).

Importing this package never imports torch; the GPU code (``worker``, ``spade``)
is only loaded inside the experiment worker process.
"""

from .api import router
from .service import Paintbrush


def create(st) -> Paintbrush:
    return Paintbrush(st)


__all__ = ["create", "router", "Paintbrush"]
