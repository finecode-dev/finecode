# docs: docs/cli.md
import asyncio
import os
import pathlib

import click

from finecode import logger_utils


def _parse_env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@click.command()
@click.option("--log-level", "log_level", default="INFO", type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False), show_default=True)
@click.option(
    "--port-file",
    "port_file",
    default=None,
    type=str,
    help="Write the listening port to this file instead of the shared discovery file. "
         "Used by dedicated instances started without --shared-server.",
)
@click.option(
    "--disconnect-timeout",
    "disconnect_timeout",
    default=30,
    type=int,
    show_default=True,
    help="Seconds to wait after the last client disconnects before shutting down.",
)
@click.option(
    "--wal",
    "wal_enabled",
    is_flag=True,
    default=None,
    help="Enable WM write-ahead log (WAL). Can also be enabled with FINECODE_WAL_ENABLED=1.",
)
def start_wm_server(
    log_level: str,
    port_file: str | None,
    disconnect_timeout: int,
    wal_enabled: bool | None,
):
    """Start the FineCode WM Server standalone (TCP JSON-RPC). Auto-stops when all clients disconnect."""
    from finecode.wm_server import wal, wm_server
    from finecode.wm_server.config import read_configs

    workspace_root = pathlib.Path.cwd()
    wm_logging = read_configs.read_wm_logging_config(workspace_root)
    wm_telemetry = read_configs.read_wm_telemetry_config(workspace_root)
    log_file_path = logger_utils.init_logger(
        log_name="wm_server", log_level=log_level, stdout=False, log_groups=wm_logging.log_groups,
        workspace_path=workspace_root,
        otlp_endpoint=wm_telemetry.otlp_endpoint,
    )
    wm_server._log_file_path = log_file_path
    port_file_path = pathlib.Path(port_file) if port_file else None

    wm_wal = read_configs.read_wm_wal_config(workspace_root)
    env_wal_enabled = _parse_env_bool("FINECODE_WAL_ENABLED", wm_wal.enabled)
    final_wal_enabled = wal_enabled if wal_enabled is not None else env_wal_enabled

    wal_config = wal.WalConfig(
        enabled=final_wal_enabled,
    )

    asyncio.run(
        wm_server.start_standalone(
            port_file=port_file_path,
            disconnect_timeout=disconnect_timeout,
            wal_config=wal_config,
            otlp_endpoint=wm_telemetry.otlp_endpoint,
        )
    )
