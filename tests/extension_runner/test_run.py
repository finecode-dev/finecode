from pathlib import Path

import pytest

from .client.finecode.extension_runner import (ExtensionRunnerService,
                                               RunActionRequest,
                                               RunActionResponse,
                                               UpdateConfigRequest)

pytestmark = pytest.mark.anyio


async def test__runs_existing_action(runner_client_channel):
    list_ws_dir_path = Path(__file__).parent.parent / "list_ws"
    cli_tool_dir_path = list_ws_dir_path / "cli_tool"
    unformatted_src_path = cli_tool_dir_path / "cli_tool" / "unformatted.py"
    update_config_request = UpdateConfigRequest(
        working_dir=cli_tool_dir_path.as_posix(), config={}
    )
    await ExtensionRunnerService.update_config(runner_client_channel, update_config_request)
    with open(unformatted_src_path, "r") as src_file:
        src_content = src_file.read()

    request = RunActionRequest(
        action_name="format", apply_on=unformatted_src_path.as_posix(), apply_on_text=src_content
    )

    response = await ExtensionRunnerService.run_action(
        channel=runner_client_channel, request=request
    )

    assert response == RunActionResponse(
        result_text="""print("a")


print("b")
"""
    )


async def test__runs_existing_action_with_multiple_subactions(runner_client_channel):
    list_ws_dir_path = Path(__file__).parent.parent / "list_ws"
    cli_tool_dir_path = list_ws_dir_path / "cli_tool"
    unformatted_src_path = cli_tool_dir_path / "cli_tool" / "unformatted_with_imports.py"
    update_config_request = UpdateConfigRequest(
        working_dir=cli_tool_dir_path.as_posix(), config={}
    )
    await ExtensionRunnerService.update_config(runner_client_channel, update_config_request)
    with open(unformatted_src_path, "r") as src_file:
        src_content = src_file.read()

    request = RunActionRequest(
        action_name="format", apply_on=unformatted_src_path.as_posix(), apply_on_text=src_content
    )

    response = await ExtensionRunnerService.run_action(
        channel=runner_client_channel, request=request
    )

    assert response == RunActionResponse(
        result_text="""import abc
import time
import typing

print("a")


print("b")
"""
    )


async def test__runs_existing_action_from_preset(runner_client_channel):
    list_ws_dir_path = Path(__file__).parent.parent / "list_ws"
    ui_app_dir_path = list_ws_dir_path / "ui_app"
    unformatted_src_path = ui_app_dir_path / "ui_app" / "unformatted.py"
    update_config_request = UpdateConfigRequest(working_dir=ui_app_dir_path.as_posix(), config={})
    await ExtensionRunnerService.update_config(runner_client_channel, update_config_request)
    with open(unformatted_src_path, "r") as src_file:
        src_content = src_file.read()

    request = RunActionRequest(
        action_name="format", apply_on=unformatted_src_path.as_posix(), apply_on_text=src_content
    )

    response = await ExtensionRunnerService.run_action(
        channel=runner_client_channel, request=request
    )

    assert response == RunActionResponse(
        result_text="""print("a")


print("b")
"""
    )
