# actions can be also integrated
from __future__ import annotations

from enum import IntEnum
from typing import Callable

from loguru import logger

_lsp_notification_send: Callable | None = None
_lsp_message_send: Callable | None = None


async def error(message: str) -> None:
    await send(message=message, message_type=UserMessageType.ERROR)


async def warning(message: str) -> None:
    await send(message=message, message_type=UserMessageType.WARNING)


async def info(message: str) -> None:
    await send(message=message, message_type=UserMessageType.INFO)


async def log(message: str) -> None:
    await send(message=message, message_type=UserMessageType.LOG)


async def debug(message: str) -> None:
    await send(message=message, message_type=UserMessageType.DEBUG)


class UserMessageType(IntEnum):
    ERROR = 1
    WARNING = 2
    INFO = 3
    LOG = 4
    DEBUG = 5


async def send(message: str, message_type: UserMessageType):
    logger.trace(f"User message: [{message_type.name}] {message}")
    if _lsp_notification_send is not None:
        await _lsp_notification_send(message, message_type.name)
    else:
        logger.error("Sender of user messages is not initialized")


__all__ = ["error", "warning", "info", "log", "debug"]
