# docs: docs/guides/developing-finecode.md
import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

from finecode_extension_runner import logs


def init_logger(
    log_name: str,
    log_level: str = "INFO",
    stdout: bool = False,
    log_groups: dict[str, str] | None = None,
) -> Path:
    venv_dir_path = Path(sys.executable).parent.parent
    logs_dir_path = venv_dir_path / "logs"

    logger.remove()
    log_file_path = logs.save_logs_to_file(
        file_path=logs_dir_path / log_name / f"{log_name}.log",
        log_level=log_level,
        stdout=stdout,
    )

    if log_groups:
        for group, level_str in log_groups.items():
            try:
                logs.set_log_level_for_group(group, logs.LogLevel[level_str.upper()])
            except KeyError:
                pass

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

    return log_file_path
