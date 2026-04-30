# docs: docs/cli.md
import os
import pathlib

import click

from finecode import logger_utils


@click.command()
@click.option("--workdir", "workdir", default=None, type=str, help="Workspace root directory")
@click.option("--log-level", "log_level", default="INFO", type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False), show_default=True)
@click.option(
    "--wm-port-file",
    "wm_port_file",
    default=None,
    type=str,
    help="Start a dedicated WM server and write its port to this file. ",
)
def start_mcp(workdir: str | None, log_level: str, wm_port_file: str | None):
    """Start the FineCode MCP server (stdio). Connects to a running FineCode WM Server."""
    from finecode.mcp_server import server

    logger_utils.init_logger(log_name="mcp_server", log_level=log_level, stdout=False)
    workdir_path = pathlib.Path(workdir) if workdir else pathlib.Path(os.getcwd())
    port_file_path = pathlib.Path(wm_port_file) if wm_port_file else None
    server.start(workdir_path, port_file=port_file_path)
