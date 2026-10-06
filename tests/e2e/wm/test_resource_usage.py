"""The snapshot against a real WM must see the real runner's processes."""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_client import ApiClient
from tests.e2e.conftest import kill_group, start_server, wait_for_file


class _Wm:
    def __init__(self, proc, port: int) -> None:
        self.proc = proc
        self.port = port


async def _start_wm(workspace_dir: pathlib.Path, port_file: pathlib.Path) -> _Wm:
    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "120",
        ],
        cwd=workspace_dir,
    )
    assert wait_for_file(port_file, timeout=30), (
        "WM server did not write its port file within 30 s"
    )
    return _Wm(proc, int(port_file.read_text().strip()))


async def _connected(wm: _Wm, workspace_dir: pathlib.Path) -> ApiClient:
    client = ApiClient()

    async def _noop(_: object) -> None:
        pass

    client.on_notification("actions/treeChanged", _noop)
    client.on_notification("server/userMessage", _noop)
    await client.connect("127.0.0.1", wm.port, client_id="e2e")
    await client.add_dir(workspace_dir)
    return client


async def test_resource_usage_sees_running_runner(
    workspace_dir_with_er, tmp_path
) -> None:
    """A live runner must show as running in the snapshot and its peaks.

    Without this, an operator watching a real workspace would see zeros while
    work is actually executing.
    """
    import asyncio

    wm = await _start_wm(workspace_dir_with_er, tmp_path / "wm_port")
    client = await _connected(wm, workspace_dir_with_er)
    try:
        snapshot = None
        for _ in range(300):
            snapshot = await client.get_resource_usage()
            if snapshot["runners"]["running"] >= 1:
                break
            await asyncio.sleep(0.1)

        assert snapshot["runners"]["running"] >= 1
        assert snapshot["projects"]["total"] >= 1
        assert snapshot["peaks"]["runnersRunning"] >= 1
    finally:
        await client.close()
        kill_group(wm.proc)


@pytest.mark.skipif(
    __import__("sys").platform != "linux", reason="RSS smoke test needs /proc"
)
async def test_resource_usage_processes_have_rss(
    workspace_dir_with_er, tmp_path
) -> None:
    """Opt-in process rows must carry real memory figures on Linux.

    Zero or missing RSS would mean the walk attributed no process to the row
    it just reported.
    """
    wm = await _start_wm(workspace_dir_with_er, tmp_path / "wm_port")
    client = await _connected(wm, workspace_dir_with_er)
    try:
        import asyncio

        snapshot = None
        for _ in range(300):
            snapshot = await client.get_resource_usage()
            if snapshot["runners"]["running"] >= 1:
                break
            await asyncio.sleep(0.1)
        snapshot = await client.get_resource_usage(include_processes=True)

        processes = snapshot["processes"]
        assert processes["wm"]["rssMb"] > 0
        assert len(processes["runners"]) >= 1
        top = max(processes["runners"], key=lambda row: row["rssMb"])
        assert top["processCount"] >= 1
        assert top["rssMb"] > 0
    finally:
        await client.close()
        kill_group(wm.proc)
