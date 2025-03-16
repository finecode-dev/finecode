import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Type

from pygls.client import JsonRPCClient

# async def create_lsp_client_tcp(host: str, port: int) -> JsonRPCClient:
#     ls = JsonRPCClient()
#     await ls.start_tcp(host, port)
#     return ls


async def create_lsp_client_io(
    client_cls: Type[JsonRPCClient], server_cmd: str, working_dir_path: Path
) -> JsonRPCClient:
    ls = client_cls()
    splitted_cmd = shlex.split(server_cmd)
    executable, *args = splitted_cmd

    old_working_dir = os.getcwd()
    os.chdir(working_dir_path)

    # temporary remove VIRTUAL_ENV env variable to avoid starting in wrong venv
    old_virtual_env_var = os.environ.pop("VIRTUAL_ENV", None)

    creationflags = 0
    # start_new_session = True .. process has parent id of real parent, but is not
    #                             ended if parent was ended
    start_new_session = True
    if sys.platform == "win32":
        # use creationflags because `start_new_session` doesn't work on Windows
        # subprocess.CREATE_NO_WINDOW .. no console window on Windows. TODO: test
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        start_new_session = False

    await ls.start_io(
        executable,
        *args,
        start_new_session=start_new_session,
        creationflags=creationflags,
    )
    if old_virtual_env_var is not None:
        os.environ["VIRTUAL_ENV"] = old_virtual_env_var

    os.chdir(old_working_dir)  # restore original working directory
    return ls


__all__ = ["JsonRPCClient", "create_lsp_client_io"]  # "create_lsp_client_tcp",
