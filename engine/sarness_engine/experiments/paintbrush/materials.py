"""Semantic materials for God's Paintbrush.

A material is a *semantic class* (sky, water, mountain…), not a colour. The
desktop paints material indices into a label map; the visible colour is only an
interface hint. Each material is mapped separately to the label protocol of
every model that consumes the mask:

* **Live (SPADE landscapes)** — the COCO-Stuff label space (182 classes, ids as
  in the checkpoint's ``classes_list.txt``). Every material here is a real,
  trained class of that model.
* **Refine (ControlNet-Seg 1.1 + SD 1.5)** — the ADE20K protocol (150 classes,
  colours from the model card's ``ada_palette``). ADE20K has no snow, cloud or
  fog class and no separate forest class; those materials are marked
  ``approximate`` and say exactly how they are handled.

Nothing in this module imports torch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ADE20K colours used by ControlNet 1.1 seg (index = objectInfo150 Idx; 0 = unlabelled).
ADE_COLORS: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 0),
    2: (180, 120, 120),   # building, edifice
    3: (6, 230, 230),     # sky
    5: (4, 200, 3),       # tree
    7: (140, 140, 140),   # road, route
    10: (4, 250, 7),      # grass
    14: (120, 120, 70),   # earth, ground
    17: (143, 255, 140),  # mountain, mount
    18: (204, 255, 4),    # plant, flora
    22: (61, 230, 250),   # water
    27: (9, 7, 230),      # sea
    35: (255, 41, 10),    # rock, stone
    47: (160, 150, 20),   # sand
    61: (11, 200, 200),   # river
    67: (255, 0, 0),      # flower
    69: (255, 102, 0),    # hill
}

# COCO-Stuff class names for the ids used below (from the checkpoint's classes_list.txt).
COCO_NAMES: dict[int, str] = {
    95: "building-other", 96: "bush", 105: "clouds", 110: "dirt", 118: "flower", 119: "fog", 123: "grass",
    126: "hill", 134: "mountain", 147: "river", 148: "road", 149: "rock", 153: "sand", 154: "sea",
    156: "sky-other", 158: "snow", 168: "tree", 177: "water-other",
}

ADE_NAMES: dict[int, str] = {
    0: "unlabelled", 2: "building", 3: "sky", 5: "tree", 7: "road", 10: "grass", 14: "earth", 17: "mountain",
    18: "plant", 22: "water", 27: "sea", 35: "rock", 47: "sand", 61: "river", 67: "flower", 69: "hill",
}


@dataclass(frozen=True)
class Material:
    id: str
    label: str
    color: str            # interface colour only
    group: str
    coco_id: int          # SPADE (COCO-Stuff) class
    ade_index: int        # ControlNet-Seg (ADE20K) class; 0 = left unlabelled
    prompt: str           # words added to the refine prompt when the material is present
    live: str = "native"  # native | experimental
    refine: str = "native"  # native | approximate
    note: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def public(self, index: int) -> dict[str, Any]:
        d = asdict(self)
        d.pop("keywords")
        d["index"] = index
        d["coco_name"] = COCO_NAMES[self.coco_id]
        d["ade_name"] = ADE_NAMES[self.ade_index]
        d["ade_color"] = "#%02x%02x%02x" % ADE_COLORS[self.ade_index]
        return d


# Order is part of the wire protocol: a material's index is its position + 1
# (0 means "nothing painted" inside a layer and is never sent for inference).
MATERIALS: tuple[Material, ...] = (
    Material("sky", "Sky", "#8ec5f4", "Sky", 156, 3, "a clear sky"),
    Material("clouds", "Clouds", "#e6ebf2", "Sky", 105, 3, "clouds",
             refine="approximate", note="ADE20K has no cloud class: refined as sky, with \"clouds\" in the prompt."),
    Material("fog", "Fog", "#b9c0ca", "Sky", 119, 0, "fog, mist",
             refine="approximate", note="ADE20K has no fog class: left unlabelled for the refiner, with \"fog\" in the prompt."),
    Material("water", "Water", "#3f8fd2", "Water", 177, 22, "a calm lake with reflections"),
    Material("ocean", "Ocean", "#1c4f94", "Water", 154, 27, "the ocean"),
    Material("river", "River", "#55b8dc", "Water", 147, 61, "a river"),
    Material("grass", "Grass", "#79c25a", "Vegetation", 123, 10, "green grass"),
    Material("trees", "Trees", "#2f7d34", "Vegetation", 168, 5, "trees"),
    Material("forest", "Forest", "#1d5527", "Vegetation", 168, 5, "a dense forest",
             note="Neither model has a separate forest class: painted as Trees, with \"dense forest\" in the refine prompt."),
    Material("bushes", "Bushes", "#6f8f2c", "Vegetation", 96, 18, "bushes"),
    Material("flowers", "Flowers", "#e46fae", "Vegetation", 118, 67, "wildflowers"),
    Material("mountains", "Mountains", "#7a6e64", "Terrain", 134, 17, "mountains"),
    Material("hills", "Hills", "#a5b061", "Terrain", 126, 69, "rolling hills"),
    Material("rocks", "Rocks", "#8d8d8d", "Terrain", 149, 35, "rocks"),
    Material("snow", "Snow", "#ffffff", "Terrain", 158, 0, "snow",
             refine="approximate", note="ADE20K has no snow class: left unlabelled for the refiner, with \"snow\" in the prompt."),
    Material("sand", "Sand", "#e7d39e", "Terrain", 153, 47, "sand"),
    Material("dirt", "Dirt", "#8a5a30", "Terrain", 110, 14, "bare earth"),
    Material("road", "Road", "#5b5d61", "Built", 148, 7, "a road"),
    Material("buildings", "Buildings", "#b4533d", "Built", 95, 2, "buildings", live="experimental",
             note="The landscape training photos contain few buildings, so the live model renders them loosely."),
)

INDEX: dict[str, int] = {m.id: i + 1 for i, m in enumerate(MATERIALS)}
MAX_INDEX = len(MATERIALS)


def by_index(index: int) -> Material:
    return MATERIALS[index - 1]


def public_list() -> list[dict[str, Any]]:
    return [m.public(i + 1) for i, m in enumerate(MATERIALS)]


def coco_lut() -> list[int]:
    """index → COCO-Stuff id (index 0 maps to sky-other; it is rejected before inference)."""
    return [156] + [m.coco_id for m in MATERIALS]


def ade_lut() -> list[tuple[int, int, int]]:
    """index → ADE20K colour for the ControlNet control image."""
    return [ADE_COLORS[0]] + [ADE_COLORS[m.ade_index] for m in MATERIALS]


def color_lut() -> list[tuple[int, int, int]]:
    """index → interface colour (what the user painted)."""
    out = [(0, 0, 0)]
    for m in MATERIALS:
        h = m.color.lstrip("#")
        out.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
    return out
