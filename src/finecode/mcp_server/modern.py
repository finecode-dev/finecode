"""Modern MCP protocol front (revision 2026-07-28 and later).

Ownership: front. Will own per-request ``_meta`` validation, response encoding
and MCP outbound effects for stateless clients. Reaches the core only through
explicit service arguments, like the legacy front, and imports neither the
legacy front, the assembly nor the CLI.

Not assembled yet: ``server.start`` serves the legacy front only, and this front
registers no methods. Its behaviour is added step by step; see issue #903.
"""

from __future__ import annotations

from finecode.mcp_server.core import ToolService
from finecode_jsonrpc import JsonRpcServerSession

__all__ = ["ModernFront"]


class ModernFront:
    """The 2026-07-28 protocol surface: stateless, per-request metadata."""

    def __init__(self, session: JsonRpcServerSession) -> None:
        self._session = session

    def register(self, service: ToolService) -> None:
        pass
