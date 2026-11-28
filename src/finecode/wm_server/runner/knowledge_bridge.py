"""Slot through which whoever owns the fact store serves the runner layer.

``knowledge/query`` and ``knowledge/registerSchema`` arrive on the **runner**'s
JSON-RPC client. The code that answers them lives in
``services/knowledge_service.py``, because it owns the fact store and the memo
table -- and services sit *above* the runner in the WM's layer stack. See ADR-0072
for why this is a slot the owner fills on import rather than an upward import.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from finecode.wm_server import context

__all__ = ["KnowledgeHandlers", "handlers", "install", "reset"]


class KnowledgeHandlers(typing.Protocol):
    """What the runner needs from whoever owns the store."""

    async def register_schema(self, snapshot: dict) -> bool:
        """Install *snapshot* as the schema the WM's store is read against."""

    async def run_query(
        self,
        ws_context: context.WorkspaceContext,
        query: dict,
        *,
        mode: str,
        limit: int | None,
    ) -> dict:
        """Execute a serialized query and return ``{"rows": ..., "freshness": ...}``."""

    async def fetch_records(
        self, ws_context: context.WorkspaceContext, refs: list[dict]
    ) -> dict:
        """Read whole entity records and return ``{"records": [...]}``.

        The read a query cannot express,
        routed here for the same reason ``run_query`` is: it touches the store, so
        it happens where the store is.
        """


_installed: KnowledgeHandlers | None = None


def install(implementation: KnowledgeHandlers) -> None:
    """Nominate *implementation* as the answer to knowledge requests from an ER."""
    global _installed
    _installed = implementation


def reset() -> None:
    """Forget the installed implementation. Tests only."""
    global _installed
    _installed = None


def handlers() -> KnowledgeHandlers | None:
    """The installed implementation, or ``None`` if the service was never imported.

    ``None`` is a real state rather than a defect: a WM built without the
    knowledge service answers ``knowledge/query`` with a method error, which is
    what an ER asking a WM that cannot serve it should hear.
    """
    return _installed
