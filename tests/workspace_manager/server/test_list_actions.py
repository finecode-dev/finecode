from pathlib import Path

from .client.finecode.workspace_manager import (AddWorkspaceDirRequest,
                                                ListActionsRequest,
                                                ListActionsResponse,
                                                WorkspaceManagerService)


async def test__returns_correct_list(client_channel):
    # ws dir 'list_ws':
    # - project 'backend'
    # - directory 'libraries'
    # -- project 'domain'
    # --- action
    # --- preset
    # ---- action
    # ---- action
    # - project 'cli_tool'
    # -- local action
    # -- action from project 'black'
    list_ws_dir_path = Path(__file__).parent.parent / "list_ws"
    add_ws_dir_request = AddWorkspaceDirRequest(dir_path=list_ws_dir_path.as_posix())
    await WorkspaceManagerService.add_workspace_dir(client_channel, add_ws_dir_request)
    request = ListActionsRequest(parent_node_id="")

    response = await WorkspaceManagerService.list_actions(channel=client_channel, request=request)

    assert response == ListActionsResponse(nodes=[])
