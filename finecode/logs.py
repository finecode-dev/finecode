import enum
import io
import sys
from pathlib import Path

from loguru import logger


class LogLevel(enum.IntEnum):
    TRACE = 5
    DEBUG = 10
    INFO = 20
    SUCCESS = 25
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


log_level_by_group: dict[str, LogLevel | None] = {}


def filter_logs(record):
    module_name = record["name"]
    if module_name in log_level_by_group:
        module_log_level = log_level_by_group[module_name]
        if module_log_level is not None:
            log_level_number = record["level"].no
            if log_level_number >= module_log_level.value:
                return True
            else:
                return False
        else:
            return False
    else:
        return True


def save_logs_to_file(
    file_path: Path,
    log_level: str = "INFO",
    rotation: str = "10 MB",
    retention=3,
    stdout: bool = True,
):
    if stdout is True:
        if isinstance(sys.stdout, io.TextIOWrapper):
            # reconfigure to be able to handle special symbols
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

        logger.add(sys.stdout, level=log_level)

    logger.add(
        str(file_path),
        rotation=rotation,
        retention=retention,
        level=log_level,
        # set encoding explicitly to be able to handle special symbols
        encoding="utf8",
        filter=filter_logs,
    )
    logger.trace(f"Log file: {file_path}")


def set_log_level_for_group(group: str, level: LogLevel | None):
    log_level_by_group[group] = level


def reset_log_level_for_group(group: str):
    if group in log_level_by_group:
        del log_level_by_group[group]
