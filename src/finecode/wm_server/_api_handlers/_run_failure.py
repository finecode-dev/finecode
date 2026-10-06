# docs: docs/guides/wm-server-internals.md
"""Log a run failure with host memory state and decorate its client message."""

from __future__ import annotations

from loguru import logger

from finecode.wm_server import host_pressure

__all__ = ["client_message"]


def client_message(log_prefix: str, message: str) -> str:
    """Log a run failure the WM is about to send to a client, with host memory
    state; return the message to send."""
    try:
        reading = host_pressure.read_memory_pressure()
    except Exception:
        logger.error(f"{log_prefix}: {message}")
        return message
    logger.bind(**reading.fields()).error(
        f"{log_prefix}: {message}; host: {reading.describe()}"
    )
    if reading.active:
        return f"{message} [host under memory pressure: {reading.describe()}]"
    return message
