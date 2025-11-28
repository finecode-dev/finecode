import inspect
import logging
import socket
import sys

from loguru import logger

import finecode_extension_runner.global_state as global_state
import finecode_extension_runner.lsp_server as extension_runner_lsp
from finecode_extension_runner import logs


def start_runner_sync(env_name: str) -> None:
    logger.remove()
    # disable logging raw messages
    # TODO: make configurable
    # logger.configure(activation=[("pygls.protocol.json_rpc", False)])
    # ~~extension runner communicates with workspace manager with tcp, we can print logs
    # to stdout as well~~. See README.md
    assert global_state.project_dir_path is not None
    logs.save_logs_to_file(
        file_path=global_state.project_dir_path
        / ".venvs"
        / env_name
        / "logs"
        / "runner.log",
        log_level=global_state.log_level,
        stdout=True,
    )

    # pygls uses standard python logger, intercept it and pass logs to loguru
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Get corresponding Loguru level if it exists.
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where originated the logged message.
            frame, depth = inspect.currentframe(), 0
            while frame and (
                depth == 0 or frame.f_code.co_filename == logging.__file__
            ):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # TODO: make configurable
    logs.set_log_level_for_group(
        "finecode_extension_runner.impls.file_manager", logs.LogLevel.WARNING
    )
    logs.set_log_level_for_group(
        "finecode_extension_runner.impls.inmemory_cache", logs.LogLevel.WARNING
    )

    logger.info(f"Python executable: {sys.executable}")
    logger.info(f"Project path: {global_state.project_dir_path}")

    server = extension_runner_lsp.create_lsp_server()
    # asyncio.run(server.start_io_async())
    port = _find_free_port()
    server.start_tcp(host="127.0.0.1", port=port)


def _find_free_port() -> int:
    """Find and return a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port
