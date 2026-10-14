"""REQUIREMENT: the ER spawn must inherit the WM's environment (minus VIRTUAL_ENV).

CI sets UV_CACHE_DIR on the WM process; uv only honours it inside the ERs if the
client forwards os.environ. This pins the one place that could silently strip it.
"""

from __future__ import annotations

import pathlib

from finecode_jsonrpc import client as jc


async def test_start_forwards_os_environ_and_drops_virtual_env(
    monkeypatch,
) -> None:
    client = jc.JsonRpcClient(message_types={}, readable_id="t")
    captured: dict = {}

    async def _fake_start_server(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(jc.JsonRpcClient, "_start_server", _fake_start_server)
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/uv-cache")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/some-venv")

    await client.start(
        server_cmd=["true"],
        working_dir_path=pathlib.Path("."),
        io_thread=None,  # never reached: _start_server is stubbed
        debug_port_future=None,
        connect=False,
    )

    assert captured["env"]["UV_CACHE_DIR"] == "/tmp/uv-cache"
    assert "VIRTUAL_ENV" not in captured["env"]
