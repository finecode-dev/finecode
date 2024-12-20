from pygls.client import JsonRPCClient


async def create_lsp_client_tcp(host: str, port: int) -> JsonRPCClient:
    ls = JsonRPCClient()
    await ls.start_tcp(host, port)
    return ls


async def create_lsp_client_io(server_cmd: str) -> JsonRPCClient:
    ls = JsonRPCClient()
    splitted_cmd = server_cmd.split(' ')
    executable, *args = splitted_cmd
    await ls.start_io(executable, *args)
    return ls


__all__ = ['JsonRPCClient', 'create_lsp_client_tcp', 'create_lsp_client_io']