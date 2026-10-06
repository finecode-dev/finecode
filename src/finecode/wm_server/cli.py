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
@click.option(
    "--log-level",
    "log_level",
    default="INFO",
    type=click.Choice(
        ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    show_default=True,
)
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
    "--keep-alive",
    "keep_alive",
    is_flag=True,
    default=False,
    help="Never auto-stop: neither when no client connects after startup nor when "
    "the last one disconnects. For a server whose lifetime something else owns "
    "(a devcontainer, a supervisor). 'server/shutdown' still stops it.",
)
@click.option(
    "--detach",
    "detach",
    is_flag=True,
    default=False,
    help="Start the shared server as a background process and exit, doing nothing "
    "if one is already listening. Without --keep-alive it stops again once the "
    "disconnect timeout expires with nobody attached. Cannot be combined with "
    "--port-file.",
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
    keep_alive: bool,
    detach: bool,
    wal_enabled: bool | None,
):
    """Start the FineCode WM Server standalone (TCP JSON-RPC).

    Runs in the foreground and auto-stops once the last client disconnects,
    unless --keep-alive is given. With --detach, the same server is spawned as
    a background process instead and this command returns.
    """
    from finecode.wm_server import wal, wm_lifecycle, wm_server
    from finecode.wm_server.config import read_configs

    workspace_root = pathlib.Path.cwd()

    if detach:
        # --port-file addresses a dedicated instance, which is started by the
        # client that owns it and found through that file rather than through
        # discovery. There is nothing for "already listening" to mean here.
        if port_file is not None:
            raise click.UsageError("--detach cannot be combined with --port-file.")
        wm_lifecycle.ensure_running(
            workspace_root,
            log_level=log_level,
            keep_alive=keep_alive,
            disconnect_timeout=disconnect_timeout,
            wal_enabled=bool(wal_enabled),
        )
        if not wm_lifecycle.is_running():
            # Not reachable is not the same as failed: a cold start can take
            # longer than the poll, and the server that follows is the one the
            # next client will attach to.
            raise click.ClickException(
                "FineCode WM server was not reachable within "
                f"{wm_lifecycle.STARTUP_READY_TIMEOUT_SECONDS}s; it may still be "
                f"starting. See {wm_lifecycle.startup_stderr_log_path()}."
            )
        return

    wm_logging = read_configs.read_wm_logging_config(workspace_root)
    wm_telemetry = read_configs.read_wm_telemetry_config(workspace_root)
    log_file_path = logger_utils.init_logger(
        log_name="wm_server",
        log_level=log_level,
        stdout=False,
        log_groups=wm_logging.log_groups,
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
            keep_alive=keep_alive,
        )
    )
