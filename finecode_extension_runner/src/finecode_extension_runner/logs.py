import enum
import io
import sys
import inspect
import logging
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
    retention: int = 3,
    stdout: bool = True,
):
    if stdout is True:
        if isinstance(sys.stdout, io.TextIOWrapper):
            # reconfigure to be able to handle special symbols
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

        logger.add(sys.stdout, level=log_level)

    # Find the file with the largest ID in the log directory
    log_dir_path = file_path.parent
    max_id = 0

    log_files_with_ids: list[tuple[int, Path]] = []
    if log_dir_path.exists():
        for log_file in log_dir_path.iterdir():
            if log_file.is_file() and log_file.suffix == '.log':
                # Extract numeric ID from the end of the filename (before extension)
                # first split by dot because loguru adds datetime after dot:
                # <stem>.<datetime>.log , we need stem without datetime
                stem = log_file.stem.split('.')[0]
                parts = stem.split('_')
                last_part = parts[-1]
                if last_part.isdigit():
                    file_id = int(last_part)
                    max_id = max(max_id, file_id)
                    log_files_with_ids.append((file_id, log_file))

    # Remove the oldest files if there are more than 10
    if len(log_files_with_ids) >= 10:
        # Sort by ID (oldest first)
        log_files_with_ids.sort(key=lambda x: x[0])
        # Keep only the 9 most recent, so after adding the new one we'll have 10
        files_to_remove = log_files_with_ids[:-9]
        for _, log_file in files_to_remove:
            try:
                log_file.unlink()
                logger.trace(f"Removed old log file: {log_file}")
            except Exception as e:
                logger.warning(f"Failed to remove old log file {log_file}: {e}")

    # Get next ID for new log file
    next_id = max_id + 1

    # Update file_path with the new ID
    file_path_with_id = file_path.with_stem(file_path.stem + '_' + str(next_id))

    logger.add(
        str(file_path_with_id),
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


def setup_logging(log_level: str, log_file_path: Path) -> None:
    logger.remove()

    # disable logging raw messages
    # TODO: make configurable
    # disable logging all raw sent messages
    logger.configure(activation=[("pygls.protocol.json_rpc", False)])

    # ~~extension runner communicates with workspace manager with tcp, we can print logs
    # to stdout as well~~. See README.md
    save_logs_to_file(
        file_path=log_file_path,
        log_level=log_level,
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
    set_log_level_for_group(
        "finecode_extension_runner.impls.file_manager", LogLevel.WARNING
    )
    set_log_level_for_group(
        "finecode_extension_runner.impls.inmemory_cache", LogLevel.WARNING
    )


__all__ = ["save_logs_to_file", "set_log_level_for_group", "reset_log_level_for_group", "setup_logging"]
