from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import falkordb


class FalkorDBNotInitializedError(Exception):
    """Raised when get_client or get_graph is called before init_falkordb has run."""


class IFalkorDBClientProvider(Protocol):
    def get_client(self) -> falkordb.FalkorDB:
        """Return the connected FalkorDB client.

        Raises:
            FalkorDBNotInitializedError: if init_falkordb has not been run.
        """
        ...

    def get_graph(self) -> falkordb.Graph:
        """Return the graph for the name configured at init time.

        Raises:
            FalkorDBNotInitializedError: if init_falkordb has not been run.
        """
        ...
