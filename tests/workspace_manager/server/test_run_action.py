import asyncio
from pathlib import Path

import pytest

from .client.finecode.workspace_manager import (AddWorkspaceDirRequest,
                                                ListActionsRequest,
                                                RunActionRequest,
                                                RunActionResponse,
                                                WorkspaceManagerService)

pytestmark = pytest.mark.anyio


async def test__runs_action_in_project(client_channel):
    # workspace with single project
    # TODO: move in fixture
    list_ws_dir_path = Path(__file__).parent.parent.parent / "list_ws"
    cli_tool_root_dir_path = list_ws_dir_path / "cli_tool"
    unformatted_src_path = cli_tool_root_dir_path / "cli_tool" / "unformatted.py"
    add_ws_dir_request = AddWorkspaceDirRequest(dir_path=cli_tool_root_dir_path.as_posix())
    await WorkspaceManagerService.add_workspace_dir(client_channel, add_ws_dir_request)

    # workspace manager expects first list call to cache actions
    request = ListActionsRequest(parent_node_id="")
    await WorkspaceManagerService.list_actions(channel=client_channel, request=request)

    await asyncio.sleep(5)

    request = RunActionRequest(
        action_node_id=f"{cli_tool_root_dir_path.as_posix()}::format",
        apply_on=unformatted_src_path.as_posix(),
        apply_on_text="",
    )
    response = await WorkspaceManagerService.run_action(channel=client_channel, request=request)

    assert response == RunActionResponse(
        result_text="""print("a")


print("b")
"""
    )


async def test__runs_general_action_in_project(client_channel):
    # workspace with single project
    # TODO: move in fixture
    list_ws_dir_path = Path(__file__).parent.parent.parent / "list_ws"
    cli_tool_root_dir_path = list_ws_dir_path / "cli_tool"
    unformatted_src_path = cli_tool_root_dir_path / "cli_tool" / "unformatted.py"
    add_ws_dir_request = AddWorkspaceDirRequest(dir_path=cli_tool_root_dir_path.as_posix())
    await WorkspaceManagerService.add_workspace_dir(client_channel, add_ws_dir_request)

    # workspace manager expects first list call to cache actions
    request = ListActionsRequest(parent_node_id="")
    await WorkspaceManagerService.list_actions(channel=client_channel, request=request)

    await asyncio.sleep(5)

    request = RunActionRequest(
        action_node_id="format", apply_on=unformatted_src_path.as_posix(), apply_on_text=""
    )
    response = await WorkspaceManagerService.run_action(channel=client_channel, request=request)

    assert response == RunActionResponse(
        result_text="""print("a")


print("b")
"""
    )


async def test__runs_action_in_one_of_projects(client_channel):
    # workspace with multiple projects
    # TODO: move in fixture
    list_ws_dir_path = Path(__file__).parent.parent.parent / "list_ws"
    cli_tool_root_dir_path = list_ws_dir_path / "cli_tool"
    unformatted_src_path = cli_tool_root_dir_path / "cli_tool" / "unformatted.py"
    add_ws_dir_request = AddWorkspaceDirRequest(dir_path=list_ws_dir_path.as_posix())
    await WorkspaceManagerService.add_workspace_dir(client_channel, add_ws_dir_request)

    # workspace manager expects first list call to cache actions
    request = ListActionsRequest(parent_node_id="")
    await WorkspaceManagerService.list_actions(channel=client_channel, request=request)

    # await asyncio.sleep(5)

    request = RunActionRequest(
        action_node_id=f"{cli_tool_root_dir_path.as_posix()}::format",
        apply_on=unformatted_src_path.as_posix(),
        apply_on_text="",
    )
    response = await WorkspaceManagerService.run_action(channel=client_channel, request=request)

    assert response == RunActionResponse(
        result_text="""print("a")


print("b")
"""
    )
