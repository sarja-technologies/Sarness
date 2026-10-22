import io
import time

import pytest
from PIL import Image

from sarness_engine.config import load_config, save_config
from sarness_engine.db import Database
from sarness_engine.models.registry import ModelRegistry, gguf_metadata
from sarness_engine.settings import Settings
from sarness_engine.storage import Storage, StorageError, _sniff_mime


@pytest.fixture
def cfg(tmp_path):
    c = load_config(tmp_path / "data")
    c.ensure_dirs()
    return c


def test_config_token_roundtrip(cfg):
    token = cfg.rotate_token()
    save_config(cfg)
    again = load_config(cfg.root)
    assert again.check_token(token)
    assert not again.check_token(token + "x")
    assert not again.check_token(None)
    assert token not in (cfg.root / "config.json").read_text()
    assert oct((cfg.root / "config.json").stat().st_mode)[-3:] == "600"


def test_db_migrations_idempotent(tmp_path):
    db = Database(tmp_path / "x.db")
    v = db.schema_version
    db.close()
    db2 = Database(tmp_path / "x.db")
    assert db2.schema_version == v >= 1


def test_settings_redacts_secrets(tmp_path):
    s = Settings(Database(tmp_path / "s.db"))
    s.update({"hf_token": "hf_secret", "llm_endpoints": [{"id": "e1", "name": "x", "base_url": "http://h/v1", "api_key": "k"}]})
    pub = s.all_public()
    assert pub["hf_token"] is True
    assert pub["llm_endpoints"][0]["api_key"] is True
    # Sending back the redacted value keeps the stored key.
    s.update({"llm_endpoints": pub["llm_endpoints"]})
    assert s.get("llm_endpoints")[0]["api_key"] == "k"
    with pytest.raises(KeyError):
        s.update({"nope": 1})
    with pytest.raises(TypeError):
        s.update({"vram_headroom_mb": "lots"})


def test_storage_path_escape(cfg):
    st = Storage(cfg, Database(cfg.db_path))
    with pytest.raises(StorageError):
        st.resolve("../../etc/passwd")


def test_upload_sniffing_and_rejection(cfg):
    st = Storage(cfg, Database(cfg.db_path))
    buf = io.BytesIO()
    Image.new("RGB", (32, 16), "purple").save(buf, "PNG")
    row = st.save_upload(buf.getvalue(), "../../evil.png", "application/x-sh")
    assert row["mime"] == "image/png" and row["width"] == 32
    assert row["original_name"] == "evil.png"
    with pytest.raises(StorageError):
        st.save_upload(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64, "x.bin", "application/octet-stream")
    assert _sniff_mime(b"%PDF-1.7") == "application/pdf"


def test_signed_urls(cfg):
    st = Storage(cfg, Database(cfg.db_path))
    url = st.sign("f_1", ttl=60)
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert st.verify("f_1", q["exp"], q["sig"])
    assert not st.verify("f_2", q["exp"], q["sig"])
    assert not st.verify("f_1", str(int(time.time()) - 5), q["sig"])


def test_registry_discovers_layouts(cfg):
    m = cfg.models_dir
    (m / "image" / "my-sdxl").mkdir(parents=True)
    (m / "image" / "my-sdxl" / "model_index.json").write_text('{"_class_name": "StableDiffusionXLPipeline"}')
    (m / "image" / "flux").mkdir()
    (m / "image" / "flux" / "model_index.json").write_text('{"_class_name": "FluxPipeline"}')
    (m / "llm" / "r").mkdir(parents=True)
    gguf = m / "llm" / "r" / "tiny-Q4.gguf"
    _write_gguf(gguf)
    (m / "llm" / "r" / "mmproj-tiny.gguf").write_bytes(b"GGUF")
    (m / "lora" / "style.safetensors").write_bytes(b"\x00" * 16)
    reg = ModelRegistry(Database(cfg.db_path), m)
    items = {i["id"]: i for i in reg.scan()}
    assert items["image:my-sdxl"]["family"] == "sdxl" and items["image:my-sdxl"]["supported"]
    assert not items["image:flux"]["supported"]
    assert "not supported" in items["image:flux"]["unsupported_reason"]
    llm = items["gguf:tiny-q4"]
    assert llm["capabilities"]["vision"] is True
    assert llm["meta"]["architecture"] == "llama"
    assert "lora:style" in items
    gguf.unlink()
    assert "gguf:tiny-q4" not in {i["id"] for i in reg.scan()}


def _write_gguf(path, nextn=None, tensor_names=()):
    import struct

    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    kv = s("general.architecture") + struct.pack("<I", 8) + s("llama")
    kv += s("llama.context_length") + struct.pack("<I", 4) + struct.pack("<I", 4096)
    n = 2
    if nextn is not None:
        kv += s("tokenizer.ggml.model") + struct.pack("<I", 8) + s("gpt2")
        kv += s("llama.nextn_predict_layers") + struct.pack("<I", 4) + struct.pack("<I", nextn)
        n = 4
    tensors = b""
    for name in tensor_names:
        tensors += s(name) + struct.pack("<I", 1) + struct.pack("<Q", 8) + struct.pack("<IQ", 0, 0)
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensor_names), n) + kv + tensors)


def test_gguf_metadata(tmp_path):
    p = tmp_path / "m.gguf"
    _write_gguf(p)
    md = gguf_metadata(p)
    assert md == {"architecture": "llama", "context_length": 4096}
    _write_gguf(p, nextn=1, tensor_names=["blk.0.attn_q.weight", "blk.1.nextn.eh_proj.weight"])
    assert gguf_metadata(p) == {"architecture": "llama", "context_length": 4096, "nextn_layers": 1}, \
        "an embedded MTP head is reported, even after the vocabulary"
    _write_gguf(p, nextn=1, tensor_names=["blk.0.attn_q.weight"])
    assert "nextn_layers" not in gguf_metadata(p), "a header that names an MTP layer whose tensors were dropped"
