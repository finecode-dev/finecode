import io
import sys
from pathlib import Path

from loguru import logger


def save_logs_to_file(
    file_path: Path, log_level: str = "INFO", rotation: str = "10 MB", retention=3, stdout: bool = True
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
    )
    logger.info(f"Log file: {file_path}")
