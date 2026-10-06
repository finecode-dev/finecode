import os
import sys
from importlib import metadata
from pathlib import Path

import click


@click.group()
def main():
    """FineCode Extension Runner CLI"""


@main.command()
@click.option("--log-level", "log_level", type=str, default="INFO")
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option("--wal", "wal", is_flag=True, default=False)
@click.option(
    "--project-path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    required=True,
)
@click.option("--env-name", "env_name", type=str)
def start(
    log_level: str,
    debug: bool,
    wal: bool,
    project_path: Path,
    env_name: str | None,
):
    from loguru import logger  # noqa: I001, PLC0415 - lean entry point

    import finecode_extension_runner.start as runner_start  # noqa: PLC0415 - lean entry point
    from finecode_extension_runner import (  # noqa: PLC0415 - lean entry point
        er_wal,
        global_state,
        logs,
    )

    debug_port: int = 0
    if debug is True:
        import debugpy

        # avoid debugger warnings printed to stdout, they affect I/O communication
        os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"

        debug_port = runner_start._find_free_port()
        try:
            debugpy.listen(debug_port)
            click.echo(f"Debug session: 127.0.0.1:{debug_port}")
            debugpy.wait_for_client()
            debugpy.breakpoint()
        except Exception as e:
            logger.info(e)

    if env_name is None:
        click.echo("Environment name(--env-name) is required", err=True)
        sys.exit(1)

    global_state.log_level = log_level
    global_state.project_dir_path = project_path
    global_state.env_name = env_name
    wal_writer = er_wal.ErWalWriter() if wal else None

    log_file_path = (
        project_path / ".venvs" / env_name / "logs" / "runner" / "runner.log"
    )

    global_state.log_file_path = logs.setup_logging(
        log_level=log_level,
        log_file_path=log_file_path,
    )

    if debug is True:
        logger.info(f"Started debugger on 127.0.0.1:{debug_port}")

    runner_start.start_runner_sync(wal_writer=wal_writer)


@main.command("dump-action-meta")
@click.option(
    "--project-path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    required=True,
)
@click.pass_context
def dump_action_meta(ctx: click.Context, project_path: Path):
    """One-shot action-metadata dump for the WM per-venv cache.

    Protocol (caller: wm_server/runner/action_meta_dump.py): stdin takes
    {"sources": [...]}, stdout carries only the format-1 JSON document and
    stderr carries diagnostics. Exit 0 answers even with per-source failures.
    """
    import json  # noqa: PLC0415 - lean entry point

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeError) as exc:
        click.echo(f"Invalid stdin JSON: {exc}", err=True)
        ctx.exit(1)
        return
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list) or not all(
        isinstance(item, str) for item in sources
    ):
        click.echo('stdin JSON must be {"sources": [str, ...]}', err=True)
        ctx.exit(1)
        return

    # dump() imports arbitrary extension modules, which may print to stdout at
    # Python or fd level and corrupt the stdout JSON. Park fd 1 aside, point
    # both sys.stdout and fd 1 at stderr for the imports, then emit the
    # document with os.write to the parked fd: click.echo would follow the
    # redirected stdout and cannot address the parked fd.
    saved = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr  # type: ignore[assignment]
    from finecode_extension_runner import action_meta  # noqa: I001, PLC0415 - lean entry point

    document = action_meta.dump(sources, project_dir=project_path)
    os.write(saved, json.dumps(document).encode())
    os.close(saved)
    ctx.exit(0)


@main.command()
def version():
    """Show version information"""
    package_version = metadata.version("finecode_extension_runner")
    click.echo(f"FineCode Extension Runner {package_version}")


if __name__ == "__main__":
    main()
