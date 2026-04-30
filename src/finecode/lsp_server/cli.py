# docs: docs/cli.md
import asyncio

import click
from loguru import logger


@click.command()
@click.option("--log-level", "log_level", default="INFO", type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False), show_default=True)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option(
    "--socket", "tcp", default=None, type=int, help="start a TCP server"
)
@click.option(
    "--stdio", "stdio", is_flag=True, default=False, help="Use stdio communication"
)
@click.option(
    "--tcp", "tcp_auto", is_flag=True, default=False,
    help="Start TCP server on a random free port; prints 'port:<N>' to stdout for client discovery"
)
@click.option("--host", "host", default=None, help="Host for TCP and WS server")
@click.option(
    "--port", "port", default=None, type=int, help="Port for TCP and WS server"
)
def start_lsp(
    log_level: str,
    debug: bool,
    tcp: int | None,
    stdio: bool,
    tcp_auto: bool,
    host: str | None,
    port: int | None,
):
    import finecode.lsp_server.main as wm_lsp_server

    if debug is True:
        import debugpy

        try:
            debugpy.listen(5680)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    if tcp_auto:
        comm_type = "tcp"
        host = "127.0.0.1"
        port = None  # main.start() will pick a free port and print it
    elif tcp is not None:
        comm_type = "tcp"
        port = tcp
        host = "127.0.0.1"
    elif stdio is True:
        comm_type = "stdio"
    else:
        raise ValueError("Specify either --tcp, --tcp-auto or --stdio")

    asyncio.run(
        wm_lsp_server.start(comm_type=comm_type, host=host, port=port, log_level=log_level)
    )
