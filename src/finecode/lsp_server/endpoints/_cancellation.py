"""Shared cancellation-detection helper for navigation-style LSP endpoints.

Navigation endpoints call actions/run on the WM. If the underlying LSP
server (e.g. pyrefly) cancelled the request — typically because something
elsewhere in the workspace invalidated its analysis state, most commonly a
document mutating — the WM surfaces this as an ApiServerError whose code is
finecode_jsonrpc.REQUEST_CANCELLED. Endpoints must re-raise this as
asyncio.CancelledError so the outer JsonRpcServerSession (finecode_jsonrpc)
sends a genuine JSON-RPC -32800 response to the IDE, instead of silently
returning null.
"""

from __future__ import annotations

import asyncio

import finecode_jsonrpc
from loguru import logger

from finecode import wm_client


def reraise_if_cancelled(error: Exception, *, context: str) -> None:
    """Re-raise as asyncio.CancelledError if *error* signals a downstream
    LSP-server-initiated request cancellation; otherwise return normally so
    the caller falls back to its generic error handling.
    """
    if (
        isinstance(error, wm_client.ApiServerError)
        and error.code == finecode_jsonrpc.REQUEST_CANCELLED
    ):
        logger.debug(
            f"{context}: cancelled by a downstream LSP server (its analysis"
            " state was invalidated by something elsewhere in the workspace)"
        )
        raise asyncio.CancelledError(context) from error
