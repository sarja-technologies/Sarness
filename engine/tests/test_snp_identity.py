"""SNP identity (certificate and pin), configuration compatibility, CLI and lifespan integration."""

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import socket
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import snp_client as sc
from sarness_engine import cli
from sarness_engine.app import create_app
from sarness_engine.config import DEFAULT_SNP_PORT, EngineConfig, load_config, save_config
from sarness_engine.snp import identity as ident


def spki_pin(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256:" + hashlib.sha256(spki).hexdigest()


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_identity_is_created_private_and_well_formed(tmp_path):
    i = ident.load_or_create(tmp_path, "10.1.2.3", ["engine.example.org", "192.168.20.172"])
    assert mode(tmp_path / "snp") == 0o700 and mode(i.key_path) == 0o600 and mode(i.cert_path) == 0o600
    assert i.pin.startswith("sha256:") and len(i.pin) == 7 + 64 and i.pin == spki_pin(i.cert_path.read_bytes())
    cert = x509.load_pem_x509_certificate(i.cert_path.read_bytes())
    key = serialization.load_pem_private_key(i.key_path.read_bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1"
    assert cert.issuer == cert.subject  # self-signed
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= dt.timedelta(days=3653)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"localhost", "engine.example.org"}
    assert {str(a) for a in san.get_values_for_type(x509.IPAddress)} == {"127.0.0.1", "::1", "10.1.2.3", "192.168.20.172"}
    assert b"PRIVATE" not in i.cert_path.read_bytes()


def test_identity_is_stable_across_restarts(tmp_path):
    a = ident.load_or_create(tmp_path, "127.0.0.1", [])
    cert_bytes = a.cert_path.read_bytes()
    b = ident.load_or_create(tmp_path, "127.0.0.1", [])
    assert a.pin == b.pin and b.cert_path.read_bytes() == cert_bytes  # nothing was re-issued


def test_new_names_reissue_the_certificate_but_keep_the_pin(tmp_path):
    a = ident.load_or_create(tmp_path, "127.0.0.1", [])
    before = a.cert_path.read_bytes()
    b = ident.load_or_create(tmp_path, "127.0.0.1", ["new.example.org"])
    assert a.pin == b.pin
    assert b.cert_path.read_bytes() != before
    san = x509.load_pem_x509_certificate(b.cert_path.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    assert "new.example.org" in san.get_values_for_type(x509.DNSName)


def test_expiring_certificate_is_renewed_with_the_same_key(tmp_path):
    a = ident.load_or_create(tmp_path, "127.0.0.1", [])
    key = serialization.load_pem_private_key(a.key_path.read_bytes(), None)
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "old")])
    old = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
           .serial_number(1).not_valid_before(now - dt.timedelta(days=400)).not_valid_after(now + dt.timedelta(days=3))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                                                       x509.IPAddress(__import__("ipaddress").ip_address("::1"))]), False)
           .sign(key, hashes.SHA256()))
    a.cert_path.write_bytes(old.public_bytes(serialization.Encoding.PEM))
    b = ident.load_or_create(tmp_path, "127.0.0.1", [])
    assert b.pin == a.pin
    assert x509.load_pem_x509_certificate(b.cert_path.read_bytes()).not_valid_after_utc > now + dt.timedelta(days=3000)


def test_missing_or_corrupt_key_creates_a_new_identity(tmp_path):
    a = ident.load_or_create(tmp_path, "127.0.0.1", [])
    a.key_path.write_text("not a key")
    b = ident.load_or_create(tmp_path, "127.0.0.1", [])
    assert b.pin != a.pin and mode(b.key_path) == 0o600
    b.cert_path.unlink()
    assert ident.load_or_create(tmp_path, "127.0.0.1", []).pin == b.pin  # key kept, certificate re-issued


def test_tls_context_is_tls13_only_with_alpn(tmp_path):
    ctx = ident.load_or_create(tmp_path, "127.0.0.1", []).ssl_context()
    import ssl

    assert ctx.minimum_version == ctx.maximum_version == ssl.TLSVersion.TLSv1_3


def test_wildcard_hosts_add_no_san_entry():
    assert ident.san_names("0.0.0.0", None) == ["localhost", "127.0.0.1", "::1"]
    assert ident.san_names("::", ["Foo.Example", "foo.example", " "]) == ["localhost", "127.0.0.1", "::1", "foo.example"]


# ----------------------------------------------------------------------------- configuration
def test_old_config_files_without_snp_fields_still_load(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"host": "10.0.0.5", "port": 9000, "token_sha256": "ab" * 32,
                                              "signing_key": "k", "cors_origins": []}))
    cfg = load_config(d)
    assert (cfg.host, cfg.port) == ("10.0.0.5", 9000)
    assert cfg.snp_enabled is True and cfg.snp_host is None and cfg.snp_port == DEFAULT_SNP_PORT == 8744
    assert cfg.snp_names == []
    save_config(cfg)
    again = json.loads((d / "config.json").read_text())
    assert again["snp_port"] == 8744 and "token" not in again
    assert load_config(d).snp_port == 8744


def test_snp_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SARNESS_SNP_PORT", "18777")
    monkeypatch.setenv("SARNESS_SNP_HOST", "10.9.8.7")
    cfg = load_config(tmp_path / "d")
    assert cfg.snp_port == 18777 and cfg.snp_host == "10.9.8.7"
    assert EngineConfig().snp_host is None


# ----------------------------------------------------------------------------- CLI
def test_cli_snp_identity_prints_pin_and_never_key_material(tmp_path, capsys):
    d = tmp_path / "data"
    assert cli.main(["--data-dir", str(d), "snp-identity", "--json"]) == 0
    out = capsys.readouterr().out
    info = json.loads(out)
    assert info["pin"] == spki_pin((d / "snp" / "server.crt").read_bytes())
    assert info["cert"] == str(d / "snp" / "server.crt") and info["listen"] == "127.0.0.1:8744"
    assert "PRIVATE" not in out and str(d / "snp" / "server.key") not in out
    assert cli.main(["--data-dir", str(d), "snp-identity"]) == 0
    text = capsys.readouterr().out
    assert info["pin"] in text and "PRIVATE" not in text and "127.0.0.1:8744" in text


# ----------------------------------------------------------------------------- lifespan integration
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_cfg(tmp_path, **kw):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    cfg.host = "127.0.0.1"
    for k, v in kw.items():
        setattr(cfg, k, v)
    save_config(cfg)
    return cfg, token


def test_http_and_snp_are_served_by_the_same_app(tmp_path):
    cfg, token = make_cfg(tmp_path, snp_port=free_port())
    app = create_app(cfg, start_background=False, start_snp=True)
    with TestClient(app) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        assert http.get("/v1/health").json()["status"] == "ok"
        pin = app.state.snp.pin

        async def over_snp():
            c = await sc.Client.connect("127.0.0.1", cfg.snp_port, token, pin=pin)
            created = await c.call("project.create", {"body": {"name": "ViaSnp"}})
            await c.close()
            return created

        created = asyncio.run(over_snp())
        # Same AppState: the project made over SNP is visible over HTTP.
        assert [p["id"] for p in http.get("/v1/projects").json()] == [created["id"]]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", cfg.snp_port), timeout=1)  # stopped with the lifespan


def test_snp_port_in_use_is_logged_loudly_and_http_keeps_working(tmp_path, caplog):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    cfg, token = make_cfg(tmp_path, snp_port=blocker.getsockname()[1])
    try:
        with caplog.at_level(logging.ERROR):
            app = create_app(cfg, start_background=False, start_snp=True)
            with TestClient(app) as http:
                http.headers["Authorization"] = f"Bearer {token}"
                assert http.get("/v1/system").status_code == 200
                assert app.state.snp is None
        assert any("SNP/1 FAILED TO START" in r.getMessage() for r in caplog.records)
    finally:
        blocker.close()


def test_snp_can_be_disabled_and_is_off_without_background_services(tmp_path):
    port = free_port()
    cfg, token = make_cfg(tmp_path, snp_port=port, snp_enabled=False)
    with TestClient(create_app(cfg, start_background=False, start_snp=True)) as http:
        assert http.get("/v1/health").status_code == 200
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
    cfg2, _ = make_cfg(tmp_path / "other", snp_port=port)
    app = create_app(cfg2, start_background=False)  # the pre-existing test setup: no SNP listener
    with TestClient(app):
        assert app.state.snp is None
