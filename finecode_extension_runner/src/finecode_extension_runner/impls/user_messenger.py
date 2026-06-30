from __future__ import annotations

from typing import Callable

from loguru import logger as loguru_logger


class UserMessenger:
    """IUserMessenger implementation that logs via loguru and sends er/userMessage to WM."""

    def __init__(self, send_notification: Callable[[str, str], None]) -> None:
        self._send_notification = send_notification

    def warning(self, message: str) -> None:
        loguru_logger.warning(message)
        self._send_notification(message, "WARNING")

    def error(self, message: str) -> None:
        loguru_logger.error(message)
        self._send_notification(message, "ERROR")

    def info(self, message: str) -> None:
        loguru_logger.info(message)
        self._send_notification(message, "INFO")
