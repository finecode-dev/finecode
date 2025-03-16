import click
from loguru import logger

import finecode.communication_utils as communication_utils
import finecode.workspace_manager.main as workspace_manager


@click.group()
def cli(): ...


@cli.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option(
    "--socket", "tcp", default=None, type=int, help="start a TCP server"
)  # is_flag=True,
@click.option("--ws", "ws", is_flag=True, default=False, help="start a WS server")
@click.option(
    "--stdio", "stdio", is_flag=True, default=False, help="Use stdio communication"
)
@click.option("--host", "host", default=None, help="Host for TCP and WS server")
@click.option(
    "--port", "port", default=None, type=int, help="Port for TCP and WS server"
)
def start_api(
    trace: bool,
    debug: bool,
    tcp: int | None,
    ws: bool,
    stdio: bool,
    host: str | None,
    port: int | None,
):
    if debug is True:
        import debugpy

        try:
            debugpy.listen(5680)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    if tcp is not None:
        comm_type = communication_utils.CommunicationType.TCP
        port = tcp
        host = "127.0.0.1"
    elif ws is True:
        comm_type = communication_utils.CommunicationType.WS
    elif stdio is True:
        comm_type = communication_utils.CommunicationType.STDIO
    else:
        raise ValueError("Specify either --tcp, --ws or --stdio")

    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(
    #     workspace_manager.start(
    #         comm_type=comm_type, host=host, port=port, trace=trace
    #     )
    # )
    # loop.run_forever()

    # workspace manager doesn't stop with async start after closing LS client(IDE).
    # Use sync start until this problem is solved
    workspace_manager.start_sync(comm_type=comm_type, host=host, port=port, trace=trace)


if __name__ == "__main__":
    cli()
