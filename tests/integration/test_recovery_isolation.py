"""PRD-0008 — in-process integration tests for what a recovery does to the
clients that did not request it.

Drives the real dispatch loop over a real TCP loopback connection (see
``tests/integration/conftest.py``); no subprocess, no ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from finecode.wm_server import context, domain, wm_server
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services import config_reload_service


@pytest.fixture
async def two_clients(wm_client, monkeypatch: pytest.MonkeyPatch):
    """A second client on the same server, as an IDE and an agent would be."""
    from tests.integration.conftest import InProcClient

    server = await asyncio.start_server(
        lambda r, w: asyncio.ensure_future(
            wm_server._handle_client(r, w, wm_client.ws_context)
        ),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    other = InProcClient(reader, writer, wm_client.ws_context)
    try:
        yield wm_client, other
    finally:
        await other.close()
        server.close()
        await server.wait_closed()


def _seed_project(ws_context: context.WorkspaceContext, project_dir: pathlib.Path):
    ws_context.ws_projects[project_dir] = domain.CollectedProject(
        name=project_dir.name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[domain.Action(name="lint", source="pkg.Lint", handlers=[], config={})],
        services=[],
        action_handler_configs={},
    )
    ws_context.ws_projects_raw_configs[project_dir] = {"tool": {"finecode": {}}}
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_dir,
        env_name="dev_workspace",
        status=runner_client.RunnerStatus.RUNNING,
    )
    ws_context.ws_projects_extension_runners[project_dir] = {"dev_workspace": runner}


async def test_a_recovery_by_one_client_leaves_the_other_usable(
    two_clients, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PRD-0008-AC8 — a recovery narrower than replacing the server leaves every
    other connected client connected and working, with nothing to reconnect.

    An IDE and an agent are attached to the same workspace server; if the agent's
    recovery disturbed the IDE's connection, the cost of recovering would fall on
    someone who did not ask for it and cannot see why their session broke.
    """
    initiator, bystander = two_clients
    ws_context = initiator.ws_context
    _seed_project(ws_context, tmp_path)

    async def _resolve_config(projects, ws_context, **kwargs) -> None:
        return None

    async def _replace_runners(runner_working_dir_path, ws_context) -> None:
        return None

    monkeypatch.setattr(
        config_reload_service.runner_start_service,
        "start_runners_with_auto_prepare",
        _resolve_config,
    )
    monkeypatch.setattr(
        config_reload_service.runner_manager,
        "restart_extension_runners",
        _replace_runners,
    )

    assert await bystander.request("workspace/listProjects") != []

    result = await initiator.request(
        "workspace/reloadConfig", {"project": str(tmp_path)}
    )
    assert result["projects"][0]["status"] == "recovered"

    # Same connection, no reconnect: the bystander never noticed.
    projects = await bystander.request("workspace/listProjects")
    assert [project["path"] for project in projects] == [str(tmp_path)]


async def test_the_server_discloses_who_else_is_connected(two_clients) -> None:
    """PRD-0008-AC8 — a client can learn which other clients share the server
    before it does something that disturbs them.

    Replacing the server is not refused when others are attached; it is disclosed,
    which is only possible if the server will say who they are.
    """
    initiator, bystander = two_clients
    await initiator.request("client/initialize", {"clientId": "mcp-test"})
    await bystander.request("client/initialize", {"clientId": "lsp"})

    info = await initiator.request("server/getInfo")

    assert "mcp-test" in info["clients"]
    assert "lsp" in info["clients"]
    assert isinstance(info["pid"], int)
