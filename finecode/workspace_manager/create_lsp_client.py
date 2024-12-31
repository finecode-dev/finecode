import os
from pathlib import Path

from pygls.client import JsonRPCClient


async def create_lsp_client_tcp(host: str, port: int) -> JsonRPCClient:
    ls = JsonRPCClient()
    await ls.start_tcp(host, port)
    return ls


async def create_lsp_client_io(server_cmd: str, working_dir_path: Path) -> JsonRPCClient:
    ls = JsonRPCClient()
    splitted_cmd = server_cmd.split(' ')
    executable, *args = splitted_cmd

    old_working_dir = os.getcwd()
    os.chdir(working_dir_path)
    # temporary remove VIRTUAL_ENV env variable to avoid starting in wrong venv
    old_virtual_env_var = os.environ.get("VIRTUAL_ENV", "")
    os.environ["VIRTUAL_ENV"] = ""
    await ls.start_io(executable, *args)
    os.environ["VIRTUAL_ENV"] = old_virtual_env_var
    os.chdir(old_working_dir)  # restore original working directory
    return ls


__all__ = ['JsonRPCClient', 'create_lsp_client_tcp', 'create_lsp_client_io']