"""Shared fixtures for the SNP/1 tests: a real engine app (lifespan included) with its SNP listener
on an ephemeral port, driven by the independent reference client in ``snp_client.py``."""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio

from sarness_engine.app import create_app
from sarness_engine.config import load_config, save_config
from sarness_engine.snp.frames import Limits

import snp_client as sc


class SnpEnv:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token
        self.st = app.state.sarness
        self.server = app.state.snp
        self.host = "127.0.0.1"
        self.port = self.server.port
        self.pin = self.server.pin
        self._clients: list[sc.Client] = []

    async def client(self, **kw) -> sc.Client:
        kw.setdefault("pin", self.pin)
        c = await sc.Client.connect(self.host, self.port, kw.pop("token", self.token), **kw)
        self._clients.append(c)
        return c

    async def raw(self, **tls) -> sc.Raw:
        r = await sc.Raw.connect(self.host, self.port, **tls)
        self._clients.append(r)  # type: ignore[arg-type]
        return r

    async def aclose(self) -> None:
        for c in self._clients:
            try:
                if isinstance(c, sc.Client):
                    await c.close()
                else:
                    c.close()
            except Exception:  # noqa: BLE001
                pass


def make_limits(request) -> Limits:
    # Fast timings by default; individual tests override with @pytest.mark.limits(...).
    kw = dict(auth_fail_unit_s=0.01, drain_s=1.0, handshake_s=3.0)
    marker = request.node.get_closest_marker("limits")
    if marker:
        kw.update(marker.kwargs)
    return Limits(**kw)


@pytest_asyncio.fixture
async def snp(tmp_path, request):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    cfg.snp_port = 0
    cfg.host = "127.0.0.1"
    save_config(cfg)
    app = create_app(cfg, start_background=False, start_snp=True, snp_limits=make_limits(request))
    async with app.router.lifespan_context(app):
        assert app.state.snp is not None, "SNP server did not start"
        env = SnpEnv(app, token)
        try:
            yield env
        finally:
            await env.aclose()


def pytest_configure(config):
    config.addinivalue_line("markers", "limits(**kw): override SNP server Limits for one test")
    logging.getLogger("sarness.snp").setLevel(logging.DEBUG)

