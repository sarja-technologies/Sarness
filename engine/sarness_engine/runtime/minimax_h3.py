"""MiniMax H3 (video with stereo audio) on one large-memory GPU such as a DGX Spark.

The model folder holds the official MiniMaxAI/MiniMax-H3 components plus a pre-quantized FP8
transformer (unsloth/MiniMax-H3-FP8, ``transformer/MiniMax-H3-FP8.safetensors``). That checkpoint is
cut from ComfyUI's pruned layout, which stores the timestep modulation in "curve" form:
``silu(time_embedder(t))`` over a 1025-point grid of t in [0, 1] lies in a rank-8 affine subspace,
so each block's ``(96768, 2688)`` modulation projection is folded into ``(96768, 8)`` and the 8
coordinates per grid point are tabulated (``time_embedder.table``). The modules below consume that
form: interpolate the coordinates for t, project, no SiLU (it is already part of the curve).

The Qwen3-VL conditioner is the official one, truncated to the 50 decoder layers H3 reads
(``hidden_states[50]``, unnormalized) and stored in FP8, so the whole pipeline needs ~55 GB plus
activations instead of ~125 GB.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import torch
from torch import nn

from .fp8 import Fp8Linear, quantize_linears, read_safetensors, safetensors_header, set_submodule

log = logging.getLogger("sarness.minimax_h3")

TRANSFORMER_FILE = "transformer/MiniMax-H3-FP8.safetensors"
TEXT_LAYERS = 50  # MiniMax H3 conditions on the output of the conditioner's 50th decoder layer
MODALITIES = 3  # video, text and audio rows each get their own modulation row


class CurveTimeEmbedding(nn.Module):
    """Timestep in [0, 1] -> coordinates on the fitted modulation curve (linear interpolation)."""

    def __init__(self, grid: int, dim: int):
        super().__init__()
        self.register_buffer("table", torch.empty(grid, dim, dtype=torch.float32, device="meta"))

    def forward(self, timestep: torch.Tensor) -> torch.Tensor:
        grid = self.table.shape[0]
        pos = timestep.to(torch.float32).clamp(0.0, 1.0) * (grid - 1)
        lo = pos.floor().long().clamp(max=grid - 2)  # t == 1 interpolates within the last interval
        return torch.lerp(self.table[lo], self.table[lo + 1], (pos - lo).unsqueeze(-1))


class RawTimestep(nn.Module):
    """Replaces the sinusoidal projection: the curve table is indexed by the timestep itself."""

    def forward(self, timestep: torch.Tensor) -> torch.Tensor:
        return timestep


class CurveModulation(nn.Module):
    """A block's six modulation chunks per (timestep, modality) row, from curve coordinates."""

    def __init__(self, dim: int, out_features: int, hidden_size: int, out_dtype: torch.dtype):
        super().__init__()
        self.hidden_size, self.out_dtype = hidden_size, out_dtype
        self.linear = nn.Linear(dim, out_features, device="meta", dtype=torch.float32)

    def forward(self, temb: torch.Tensor) -> tuple[torch.Tensor, ...]:
        y = self.linear(temb.to(self.linear.weight.dtype))
        # The fitted projection is float32; the block stack multiplies these chunks into its own dtype.
        y = y.view(-1, MODALITIES, 6 * self.hidden_size).to(self.out_dtype)
        return y.view(-1, 6 * self.hidden_size).chunk(6, dim=-1)


class CurveNormOut(nn.Module):
    """The final shift/scale norm, from curve coordinates."""

    def __init__(self, norm: nn.Module, dim: int, out_features: int):
        super().__init__()
        self.norm = norm
        self.linear = nn.Linear(dim, out_features, device="meta", dtype=torch.float32)

    def forward(self, hidden_states: torch.Tensor, temb: torch.Tensor, timestep_indices: torch.Tensor) -> torch.Tensor:
        shift, scale = self.linear(temb.to(self.linear.weight.dtype)).chunk(2, dim=-1)
        hidden_states = self.norm(hidden_states)
        return hidden_states * (1.0 + scale.index_select(0, timestep_indices)) + shift.index_select(0, timestep_indices)


def transformer_metadata(path: Path) -> dict[str, Any]:
    """The build record of the pre-quantized transformer, validated against what this loader handles."""
    _, md = safetensors_header(path)
    meta = json.loads(md.get("unsloth_metadata") or "{}")
    problems = []
    if meta.get("transformer_class") != "MiniMaxH3Transformer3DModel":
        problems.append(f"transformer class {meta.get('transformer_class')!r}")
    if meta.get("scheme") != "fp8":
        problems.append(f"scheme {meta.get('scheme')!r}")
    if meta.get("adaln_form") != "curve" or not meta.get("curve_dim") or not meta.get("curve_grid"):
        problems.append(f"modulation form {meta.get('adaln_form')!r}")
    if "fl2va" not in str(meta.get("base_checkpoint", "")):
        problems.append("not the keyframe (fl2va) partition")
    if problems:
        raise ValueError(f"{path.name} is not a MiniMax H3 FP8 checkpoint this engine can load: " + ", ".join(problems))
    return meta


def load_transformer(model_dir: Path, device: Any) -> nn.Module:
    from accelerate import init_empty_weights
    from diffusers import MiniMaxH3Transformer3DModel

    file = model_dir / TRANSFORMER_FILE
    meta = transformer_metadata(file)
    dim, grid = int(meta["curve_dim"]), int(meta["curve_grid"])
    out_dtype = getattr(torch, str(meta.get("adaln_out_dtype") or "bfloat16"))
    config = MiniMaxH3Transformer3DModel.load_config(str(model_dir / "transformer"))
    with init_empty_weights(include_buffers=False):
        model = MiniMaxH3Transformer3DModel.from_config(config)

    state = read_safetensors(file)
    for name in sorted(k[: -len("._weight_qdata")] for k in state if k.endswith("._weight_qdata")):
        lin = model.get_submodule(name)
        if not isinstance(lin, nn.Linear):
            raise ValueError(f"{name} is quantized in the checkpoint but is not a linear layer in the model")
        set_submodule(model, name, Fp8Linear(lin.in_features, lin.out_features, bias=lin.bias is not None, device="meta"))
    for block in model.transformer_blocks:
        dense = block.adaln_proj
        block.adaln_proj = CurveModulation(dim, dense.linear.out_features, dense.hidden_size, out_dtype)
    model.norm_out = CurveNormOut(model.norm_out.norm, dim, model.norm_out.linear.out_features)
    model.time_embedder = CurveTimeEmbedding(grid, dim)
    model.time_proj = RawTimestep()

    missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
    del state
    if missing or unexpected:
        raise ValueError(f"MiniMax H3 checkpoint does not match the model: missing {missing[:5]}, unexpected {unexpected[:5]}")
    _no_meta(model, "transformer")
    return model.eval().requires_grad_(False).to(device)


def load_text_encoder(model_dir: Path, device: Any) -> nn.Module:
    from accelerate import init_empty_weights
    from transformers import AutoConfig, Qwen3VLForConditionalGeneration

    te_dir = model_dir / "text_encoder"
    config = AutoConfig.from_pretrained(str(te_dir))
    config.text_config.num_hidden_layers = TEXT_LAYERS
    with init_empty_weights(include_buffers=False):
        model = Qwen3VLForConditionalGeneration._from_config(config, dtype=torch.bfloat16)
    # Never used by H3 (it reads hidden states, not logits), and with the last layers gone the final
    # norm would turn hidden_states[50] into a normalized output it was not trained on.
    model.lm_head = nn.Identity()
    model.model.language_model.norm = nn.Identity()

    def unused(key: str) -> bool:
        m = re.match(r"model\.language_model\.layers\.(\d+)\.", key)
        return bool(m and int(m.group(1)) >= TEXT_LAYERS) or key.startswith("lm_head.") or key == "model.language_model.norm.weight"

    shards = sorted(set(json.loads((te_dir / "model.safetensors.index.json").read_text())["weight_map"].values()))
    for shard in shards:
        f = te_dir / shard
        if not f.exists():
            if any(not unused(k) for k in _shard_keys(te_dir, shard)):
                raise FileNotFoundError(f"text encoder shard {shard} is missing")
            continue  # holds only layers H3 does not use; not downloaded
        state = read_safetensors(f, skip=unused)
        _, unexpected = model.load_state_dict(state, strict=False, assign=True)
        if unexpected:
            raise ValueError(f"unexpected text encoder weights: {unexpected[:5]}")
        del state
    _no_meta(model, "text encoder")
    # The language layers carry almost all the weight; the vision tower stays bf16.
    n = quantize_linears(model.model.language_model, device)
    log.info("MiniMax H3 text encoder: %d layers, %d linears in FP8", TEXT_LAYERS, n)
    return model.eval().requires_grad_(False).to(device)


def _shard_keys(te_dir: Path, shard: str) -> list[str]:
    index = json.loads((te_dir / "model.safetensors.index.json").read_text())["weight_map"]
    return [k for k, v in index.items() if v == shard]


def _no_meta(model: nn.Module, what: str) -> None:
    left = [n for n, t in [*model.named_parameters(), *model.named_buffers()] if t.is_meta]
    if left:
        raise ValueError(f"MiniMax H3 {what}: weights missing from the checkpoint: {left[:5]}")


def load_pipeline(model_dir: str | Path, device: Any) -> Any:
    """The diffusers modular pipeline with every component loaded from ``model_dir`` (never the Hub)."""
    from diffusers import AutoencoderKLMiniMaxH3, AutoencoderKLMiniMaxH3Audio, MiniMaxH3Blocks, MiniMaxH3Scheduler
    from transformers import AutoProcessor, AutoTokenizer

    from .models import fast_load

    d = Path(model_dir)
    fast = fast_load()
    text_encoder = load_text_encoder(d, device)
    transformer = load_transformer(d, device)
    vae = AutoencoderKLMiniMaxH3.from_pretrained(str(d), subfolder="vae", dtype=torch.bfloat16, **fast).to(device)
    audio_vae = AutoencoderKLMiniMaxH3Audio.from_pretrained(str(d), subfolder="audio_vae", dtype=torch.bfloat16, **fast).to(device)
    pipe = MiniMaxH3Blocks().init_pipeline()
    pipe.update_components(
        text_encoder=text_encoder, transformer=transformer, vae=vae, audio_vae=audio_vae,
        tokenizer=AutoTokenizer.from_pretrained(str(d / "tokenizer")),
        processor=AutoProcessor.from_pretrained(str(d / "processor")),
        scheduler=MiniMaxH3Scheduler.from_pretrained(str(d), subfolder="scheduler"),
        audio_scheduler=MiniMaxH3Scheduler.from_pretrained(str(d), subfolder="audio_scheduler"),
    )
    return pipe
