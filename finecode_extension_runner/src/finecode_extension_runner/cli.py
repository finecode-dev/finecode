import os
import sys
from importlib import metadata
from pathlib import Path

import click
from loguru import logger

import finecode_extension_runner.start as runner_start
from finecode_extension_runner import global_state


@click.group()
def main():
    """FineCode Extension Runner CLI"""
    pass


@main.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option("--debug-port", "debug_port", type=int, default=5680)
@click.option(
    "--project-path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    required=True,
)
@click.option("--env-name", "env_name", type=str)
def start(
    trace: bool, debug: bool, debug_port: int, project_path: Path, env_name: str | None
):
    if debug is True:
        import debugpy

        # avoid debugger warnings printed to stdout, they affect I/O communication
        os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
        try:
            debugpy.listen(debug_port)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    if env_name is None:
        click.echo("Environment name(--env-name) is required", err=True)
        sys.exit(1)

    global_state.log_level = "INFO" if trace is False else "TRACE"
    global_state.project_dir_path = project_path
    # asyncio.run(runner_start.start_runner())

    # extension runner doesn't stop with async start after closing LS client(WM). Use
    # sync start until this problem is solved
    runner_start.start_runner_sync(env_name)


@main.command()
def version():
    """Show version information"""
    package_version = metadata.version("finecode_extension_runner")
    click.echo(f"FineCode Extension Runner {package_version}")


if __name__ == "__main__":
    main()
