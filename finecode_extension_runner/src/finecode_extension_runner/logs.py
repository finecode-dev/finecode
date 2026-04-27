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
_default_log_level: LogLevel = LogLevel.INFO


def filter_logs(record):
    module_name = record["name"]
    # Find the longest matching prefix among configured groups
    matched_level: LogLevel | None = None
    matched_len = -1
    for group, level in log_level_by_group.items():
        if (module_name == group or module_name.startswith(group + ".")) and len(group) > matched_len:
            matched_level = level
            matched_len = len(group)
    if matched_len == -1:
        return record["level"].no >= _default_log_level.value
    if matched_level is None:
        return False
    return record["level"].no >= matched_level.value


def save_logs_to_file(
    file_path: Path,
    log_level: str = "INFO",
    rotation: str = "10 MB",
    retention: int = 3,
    stdout: bool = True,
) -> Path:
    global _default_log_level
    try:
        _default_log_level = LogLevel[log_level.upper()]
    except KeyError:
        pass

    if stdout is True:
        if isinstance(sys.stdout, io.TextIOWrapper):
            # reconfigure to be able to handle special symbols
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

        logger.add(sys.stdout, level="TRACE", filter=filter_logs)

    # Find the file with the largest ID in the log directory
    log_dir_path = file_path.parent
    max_id = 0
    base_stem = file_path.stem  # e.g., "my_logfile"

    log_files_with_ids: list[tuple[int, Path]] = []
    if log_dir_path.exists():
        for log_file in log_dir_path.iterdir():
            if log_file.is_file():
                stem = log_file.stem
                # Extract numeric ID from the pattern: base_stem_<number>
                # stem might be something like "my_logfile_1.2025-03-04_12-00-00"
                if stem.startswith(base_stem + '_'):
                    # Get the part after "base_stem_"
                    id_part = stem[len(base_stem) + 1:]
                    # Split by '.' to handle datetime added by loguru
                    potential_id = id_part.split('.')[0]
                    if potential_id.isdigit():
                        file_id = int(potential_id)
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
        level="TRACE",
        # set encoding explicitly to be able to handle special symbols
        encoding="utf8",
        filter=filter_logs,
    )
    logger.trace(f"Log file: {file_path_with_id}")
    return file_path_with_id


def set_default_log_level(level: LogLevel) -> None:
    global _default_log_level
    _default_log_level = level


def set_log_level_for_group(group: str, level: LogLevel | None):
    log_level_by_group[group] = level


def reset_log_level_for_group(group: str):
    if group in log_level_by_group:
        del log_level_by_group[group]


def apply_logging_config(config: dict) -> None:
    """Apply logging config delivered via the WM→ER update_config protocol."""
    if default_level_str := config.get("defaultLevel"):
        try:
            set_default_log_level(LogLevel[default_level_str.upper()])
        except KeyError:
            logger.warning(f"Unknown log level '{default_level_str}' for defaultLevel, ignoring")

    for group, level_str in config.get("logGroups", {}).items():
        try:
            level = LogLevel[level_str.upper()]
            set_log_level_for_group(group, level)
        except KeyError:
            logger.warning(f"Unknown log level '{level_str}' for group '{group}', ignoring")


def setup_logging(log_level: str, log_file_path: Path) -> Path:
    logger.remove()

    # ~~extension runner communicates with workspace manager with tcp, we can print logs
    # to stdout as well~~. See README.md
    actual_log_file_path = save_logs_to_file(
        file_path=log_file_path,
        log_level=log_level,
        stdout=True,
    )

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

    return actual_log_file_path


__all__ = ["save_logs_to_file", "set_default_log_level", "set_log_level_for_group", "reset_log_level_for_group", "apply_logging_config", "setup_logging"]
