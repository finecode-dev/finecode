from __future__ import annotations

from typing import TYPE_CHECKING

from fine_dep_graph_falkordb.ifalkordb_client_provider import (
    FalkorDBNotInitializedError,
)

if TYPE_CHECKING:
    import falkordb


class FalkorDBClientProvider:
    def __init__(self) -> None:
        self._client: falkordb.FalkorDB | None = None
        self._graph_name: str | None = None

    def set_client(self, client: falkordb.FalkorDB, graph_name: str) -> None:
        self._client = client
        self._graph_name = graph_name

    def get_client(self) -> falkordb.FalkorDB:
        if self._client is None:
            raise FalkorDBNotInitializedError(
                "FalkorDB has not been initialized. Run init_falkordb first."
            )
        return self._client

    def get_graph(self) -> falkordb.Graph:
        if self._client is None:
            raise FalkorDBNotInitializedError(
                "FalkorDB has not been initialized. Run init_falkordb first."
            )
        return self._client.select_graph(self._graph_name)
