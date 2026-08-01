from __future__ import annotations

from typing import Any, Awaitable, Callable

from finecode_extension_api.interfaces.iknowledgestore import IKnowledgeStore

__all__ = ["KnowledgeStoreImpl"]

_REGISTER_SCHEMA = "knowledge/registerSchema"
_QUERY = "knowledge/query"
_RECORDS = "knowledge/records"


class KnowledgeStoreImpl(IKnowledgeStore):
    """Calls the WM back-channel: ``registerSchema``, ``query`` and ``records``.

    Requests and nothing else: no connection to a storage backend, no driver, no
    credentials. That is not an omission -- the ER is not supposed to be able to
    reach the store any other way.
    """

    def __init__(
        self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]
    ) -> None:
        self._send = send_request_to_wm

    async def register_schema(self, snapshot: dict) -> bool:
        result = await self._send(_REGISTER_SCHEMA, {"snapshot": snapshot})
        return bool(result.get("accepted", False))

    async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict:
        result = await self._send(
            _QUERY, {"query": query, "mode": mode, "limit": limit}
        )
        return {"rows": result["rows"], "freshness": result["freshness"]}

    async def fetch_records(self, refs: list[dict]) -> dict:
        result = await self._send(_RECORDS, {"refs": refs})
        return {"v": result["v"], "records": result["records"]}
