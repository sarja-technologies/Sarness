"""Large-model support: FP8 layers, the plain-read safetensors loader, MiniMax H3's pruned ("curve")
modulation, multi-repository downloads and hiding models that are still downloading. CPU only."""

import json

import pytest

from sarness_engine.db import Database
from sarness_engine.models import downloads as dl

torch = pytest.importorskip("torch")
from torch import nn  # noqa: E402

from sarness_engine.runtime import fp8  # noqa: E402


def test_fp8_linear_matches_full_precision():
    torch.manual_seed(0)
    lin = nn.Linear(64, 32)
    q = fp8.Fp8Linear.from_linear(lin)
    assert set(q.state_dict()) == {"_weight_qdata", "_weight_scale", "bias"}, "torchao Float8Tensor layout"
    assert q._weight_qdata.dtype == torch.float8_e4m3fn and q._weight_scale.shape == (32, 1)
    x = torch.randn(3, 5, 64)
    ref = lin(x)
    err = (q(x) - ref).norm() / ref.norm()
    assert q(x).shape == (3, 5, 32) and err < 0.05, "e4m3 keeps 3 mantissa bits"

    root = nn.Sequential(nn.Linear(16, 16), nn.ReLU(), nn.Sequential(nn.Linear(16, 8, bias=False)))
    assert fp8.quantize_linears(root, "cpu") == 2
    assert isinstance(root[0], fp8.Fp8Linear) and isinstance(root[2][0], fp8.Fp8Linear) and root[2][0].bias is None
    assert root(torch.randn(2, 16)).dtype == torch.float32


def test_read_safetensors_matches_library(tmp_path):
    from safetensors.torch import load_file, save_file

    t = {"a": torch.randn(3, 4), "b": torch.arange(10, dtype=torch.int64), "c": torch.randn(2, 2).to(torch.bfloat16),
         "d": torch.randn(4, 4).to(torch.float8_e4m3fn), "e": torch.zeros(0)}
    save_file(t, tmp_path / "x.safetensors", metadata={"k": "v"})
    got = fp8.read_safetensors(tmp_path / "x.safetensors", skip=lambda k: k == "b")
    ref = load_file(tmp_path / "x.safetensors")
    assert set(got) == {"a", "c", "d", "e"}
    for k in got:
        assert got[k].dtype == ref[k].dtype and torch.equal(got[k].view(torch.uint8), ref[k].view(torch.uint8))
    assert fp8.safetensors_header(tmp_path / "x.safetensors")[1] == {"k": "v"}


H3_TINY = dict(num_attention_heads=2, attention_head_dim=16, hidden_size=32, num_layers=2, num_refiner_layers=1,
               ffn_dim=64, in_channels=4, audio_in_channels=4, text_dim=16, freq_dim=16, time_embed_hidden_dim=32,
               time_embed_dim=8, rope_freq_dim=4)


def _curve_checkpoint(dense, grid=65):
    """The pruned form of a dense model, built the way the published checkpoints describe it:
    silu(time_embedder(t)) over the grid = mean + B c(t); W_curve = W B; b_curve = b + W mean."""
    with torch.no_grad():
        t = torch.linspace(0, 1, grid)
        u = nn.functional.silu(dense.time_embedder(dense.time_proj(t).float()))
        mean = u.mean(0)
        _, _, vh = torch.linalg.svd(u - mean, full_matrices=False)
        basis = vh.T  # rank = time_embed_dim here, so the factorization is exact
        table = (u - mean) @ basis
        sd = {k: v.clone() for k, v in dense.state_dict().items() if not k.startswith("time_embedder.")}
        sd["time_embedder.table"] = table.float().contiguous()
        for prefix in [f"transformer_blocks.{i}.adaln_proj.linear" for i in range(len(dense.transformer_blocks))] + ["norm_out.linear"]:
            w, b = sd[prefix + ".weight"].float(), sd[prefix + ".bias"].float()
            sd[prefix + ".weight"] = (w @ basis).contiguous()
            sd[prefix + ".bias"] = (b + w @ mean).contiguous()
        for name in ["transformer_blocks.0.attn.to_q", "transformer_blocks.1.ff.net.2"]:
            q = fp8.Fp8Linear.from_linear(dense.get_submodule(name))
            del sd[name + ".weight"]
            sd[name + "._weight_qdata"], sd[name + "._weight_scale"] = q._weight_qdata, q._weight_scale
    return sd


def test_minimax_h3_curve_checkpoint_reproduces_dense_modulation(tmp_path):
    diffusers = pytest.importorskip("diffusers")
    if not hasattr(diffusers, "MiniMaxH3Transformer3DModel"):
        pytest.skip("diffusers without MiniMax H3")
    from safetensors.torch import save_file

    from sarness_engine.runtime import minimax_h3 as h3

    torch.manual_seed(0)
    dense = diffusers.MiniMaxH3Transformer3DModel(**H3_TINY).float().eval()
    (tmp_path / "transformer").mkdir()
    dense.save_config(str(tmp_path / "transformer"))
    meta = {"transformer_class": "MiniMaxH3Transformer3DModel", "scheme": "fp8", "adaln_form": "curve", "curve_dim": 8,
            "curve_grid": 65, "adaln_out_dtype": "float32", "base_checkpoint": "x/minimax_h3_fl2va_pruned_bf16.safetensors"}
    save_file(_curve_checkpoint(dense), tmp_path / h3.TRANSFORMER_FILE, metadata={"unsloth_metadata": json.dumps(meta)})

    model = h3.load_transformer(tmp_path, "cpu")
    assert isinstance(model.transformer_blocks[0].attn.to_q, fp8.Fp8Linear)
    assert isinstance(model.transformer_blocks[1].ff.net[2], fp8.Fp8Linear)
    with torch.no_grad():
        for t in (torch.tensor([0.0, 0.5, 1.0]), torch.tensor([0.3, 0.71])):  # grid points and interpolated
            temb_d = dense.time_embedder(dense.time_proj(t).float())
            temb_c = model.time_embedder(model.time_proj(t))
            for bd, bc in zip(dense.transformer_blocks, model.transformer_blocks):
                for a, b in zip(bd.adaln_proj(temb_d), bc.adaln_proj(temb_c)):
                    assert torch.allclose(a, b, atol=2e-3, rtol=2e-2), "block modulation"
            hs = torch.randn(1, 5, 32)
            idx = torch.tensor([0, 1, 1, 0, 1]) % len(t)
            assert torch.allclose(dense.norm_out(hs, temb_d, idx), model.norm_out(hs, temb_c, idx), atol=2e-3, rtol=2e-2)

    meta["base_checkpoint"] = "x/minimax_h3_ref2va_pruned_bf16.safetensors"
    save_file({"x": torch.zeros(1)}, tmp_path / "other.safetensors", metadata={"unsloth_metadata": json.dumps(meta)})
    with pytest.raises(ValueError, match="fl2va"):
        h3.transformer_metadata(tmp_path / "other.safetensors")


def test_download_from_two_repositories_and_hidden_until_complete(tmp_path, monkeypatch):
    from sarness_engine.models.registry import ModelRegistry

    trees = {"org/main": [{"path": "model_index.json", "size": 2}, {"path": "vae/w.safetensors", "size": 3},
                          {"path": "transformer/big.safetensors", "size": 9}],
             "other/fp8": [{"path": "M-FP8.safetensors", "size": 4}, {"path": "README.md", "size": 1}]}
    monkeypatch.setattr(dl, "list_repo_files", lambda repo, token=None, revision="main": trees[repo])
    db = Database(tmp_path / "d.db")
    dm = dl.DownloadManager(db, lambda *a: None, lambda: "", lambda r: None)
    dm._pump = lambda: None
    dest = tmp_path / "models" / "video" / "org--main"
    row = dm.create(model_id="m", name="M", category="video", repo="org/main", dest=dest,
                    patterns=["model_index.json", "vae/*"],
                    sources=[{"repo": "other/fp8", "files": ["M-FP8.safetensors"], "subdir": "transformer"}])
    extra = row["files"][-1]
    assert [f["path"] for f in row["files"]] == ["model_index.json", "vae/w.safetensors", "transformer/M-FP8.safetensors"]
    assert extra["repo"] == "other/fp8" and extra["src"] == "M-FP8.safetensors" and row["total_bytes"] == 9

    (dest / "model_index.json").write_text('{"_class_name": "WanPipeline"}')
    reg = ModelRegistry(db, tmp_path / "models")
    assert not [m for m in reg.scan() if m["category"] == "video"], "a model is not listed while it downloads"
    dl._drop_marker(dest, row["id"])
    assert [m for m in reg.scan() if m["category"] == "video"]

    llm = tmp_path / "models" / "llm" / "org--gguf"
    llm.mkdir(parents=True)
    for n in ("big-00001-of-00002.gguf", "small.gguf"):
        (llm / n).write_bytes(b"GGUF" + b"\0" * 60)
    dl._write_marker(llm, "d_x", [{"path": "big-00002-of-00002.gguf"}])
    names = {m["path"].rsplit("/", 1)[-1] for m in reg.scan() if m["category"] == "llm"}
    assert "small.gguf" in names and "big-00001-of-00002.gguf" not in names, "a split GGUF waits for every shard"
