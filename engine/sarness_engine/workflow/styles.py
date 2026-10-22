"""Prompt styles: reusable wording that steers any image model towards a look.

A style wraps the user's prompt with a description of the look and, for models that take one, adds a
negative prompt. The newer models (Qwen-Image, Z-Image, FLUX.2) read prompts with an LLM text encoder, so
styles are written as plain sentences rather than tag lists.
"""

from __future__ import annotations

from typing import Any

STYLES: dict[str, dict[str, Any]] = {
    "iphone": {
        "label": "iPhone / candid photo",
        "description": "Looks like an ordinary photo from a recent iPhone: natural light, real skin, everyday "
                       "framing, no studio polish.",
        "prefix": "An unedited, candid smartphone photo taken on a recent iPhone with the main wide camera.",
        "suffix": (
            "Shot handheld in ordinary available light, slightly imperfect framing as in a quick snapshot. "
            "Typical iPhone Smart HDR processing: bright but natural exposure, lifted shadows, true-to-life "
            "colors, a little digital sharpening and faint noise in darker areas. Everything mostly in focus "
            "with deep depth of field, no artificial background blur. People look like real people: natural "
            "skin texture with visible pores, fine hairs, small blemishes and uneven tone, slight facial "
            "asymmetry, relaxed natural expressions, realistic hands and hair, everyday clothes with creases. "
            "Real, lived-in surroundings. Not retouched, not posed, not cinematic."
        ),
        "negative": (
            "studio lighting, cinematic color grading, dramatic rim light, teal and orange, bokeh, shallow depth "
            "of field, airbrushed skin, plastic skin, beauty filter, smooth waxy face, perfect symmetry, model "
            "pose, glamour, fashion editorial, CGI, 3D render, illustration, painting, oversaturated, HDR halos, "
            "over-sharpened, watermark, text overlay"
        ),
    },
}


def style_options() -> list[dict[str, str]]:
    return [{"value": "none", "label": "None"}] + [{"value": k, "label": v["label"]} for k, v in STYLES.items()]


def apply_style(style: str | None, prompt: str, negative: str) -> tuple[str, str]:
    """Returns the prompt and negative prompt with ``style`` applied; unknown or empty styles change nothing."""
    s = STYLES.get(style or "")
    if not s:
        return prompt, negative
    text = " ".join(x for x in (s["prefix"], prompt.strip(), s["suffix"]) if x)
    neg = ", ".join(x for x in (negative.strip(), s["negative"]) if x)
    return text, neg
