"""
Client API used only internally in runner manager or other modules of this package. They
are not intended to be used in higher layers.
"""

from loguru import logger

from finecode.runner import _internal_client_types
from finecode.runner.jsonrpc_client import client as jsonrpc_client


async def initialize(
    client: jsonrpc_client.JsonRpcClient,
    client_process_id: int,
    client_name: str,
    client_version: str,
) -> None:
    logger.debug(f"Send initialize to server {client.readable_id}")
    await client.send_request(
        method=_internal_client_types.INITIALIZE,
        params=_internal_client_types.InitializeParams(
            process_id=client_process_id,
            capabilities=_internal_client_types.ClientCapabilities(),
            client_info=_internal_client_types.ClientInfo(
                name=client_name, version=client_version
            ),
            trace=_internal_client_types.TraceValue.Verbose,
        ),
        timeout=20,
    )


async def notify_initialized(client: jsonrpc_client.JsonRpcClient) -> None:
    logger.debug(f"Notify initialized {client.readable_id}")
    client.notify(
        method=_internal_client_types.INITIALIZED,
        params=_internal_client_types.InitializedParams(),
    )


async def cancel_request(
    client: jsonrpc_client.JsonRpcClient, request_id: int | str
) -> None:
    logger.debug(f"Cancel request {request_id} | {client.readable_id}")
    client.notify(
        method=_internal_client_types.CANCEL_REQUEST,
        params=_internal_client_types.CancelParams(id=request_id),
    )


async def shutdown(
    client: jsonrpc_client.JsonRpcClient,
) -> None:
    logger.debug(f"Send shutdown to server {client.readable_id}")
    await client.send_request(method=_internal_client_types.SHUTDOWN)


def shutdown_sync(
    client: jsonrpc_client.JsonRpcClient,
) -> None:
    logger.debug(f"Send shutdown to server  {client.readable_id}")
    client.send_request_sync(method=_internal_client_types.SHUTDOWN)


async def exit(client: jsonrpc_client.JsonRpcClient) -> None:
    logger.debug(f"Send exit to server {client.readable_id}")
    client.notify(method=_internal_client_types.EXIT)


def exit_sync(client: jsonrpc_client.JsonRpcClient) -> None:
    logger.debug(f"Send exit to server {client.readable_id}")
    client.notify(method=_internal_client_types.EXIT)
