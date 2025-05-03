import os
from pathlib import Path

import click
from loguru import logger

import finecode.extension_runner.start as runner_start
from finecode.extension_runner import global_state


@click.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option("--debug-port", "debug_port", type=int, default=5680)
@click.option(
    "--project-path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    required=True,
)
def main(trace: bool, debug: bool, debug_port: int, project_path: Path):
    if debug is True:
        import debugpy

        # avoid debugger warnings printed to stdout, they affect I/O communication
        os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
        try:
            debugpy.listen(debug_port)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    global_state.log_level = "INFO" if trace is False else "TRACE"
    global_state.project_dir_path = project_path
    # asyncio.run(runner_start.start_runner())

    # extension runner doesn't stop with async start after closing LS client(WM). Use
    # sync start until this problem is solved
    runner_start.start_runner_sync()


if __name__ == "__main__":
    main()
