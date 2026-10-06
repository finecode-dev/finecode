"""Capability accessors on the LSP server.

The endpoint must ask narrow questions of the client capabilities instead of
navigating the wire dict, and a missing or partial capability must mean
"unsupported", never an assumption.
"""

from __future__ import annotations

from finecode.lsp_server.lsp_server import LspServer
from finecode.wm_server.runner.apply_workspace_edit_bridge import (
    ResourceOperationKind,
)


def _server(capabilities: dict) -> LspServer:
    server = LspServer()
    server._client_capabilities = capabilities
    return server


def test_document_changes_support_is_declared_not_assumed() -> None:
    """A client must actually declare ``documentChanges`` before the server may
    emit it; silence means unsupported."""
    assert _server({}).supports_document_changes() is False
    assert (
        _server(
            {
                "workspace": {
                    "workspaceEdit": {"documentChanges": True},
                }
            }
        ).supports_document_changes()
        is True
    )
    assert (
        _server(
            {
                "workspace": {
                    "workspaceEdit": {"documentChanges": False},
                }
            }
        ).supports_document_changes()
        is False
    )


def test_resource_operations_default_to_empty_when_undeclared() -> None:
    """A client that lists no resource operations can perform none -- the LSP
    default, not a reason to send operations and hope."""
    assert _server({}).supported_resource_operations() == frozenset()
    assert _server(
        {
            "workspace": {
                "workspaceEdit": {"resourceOperations": ["rename"]},
            }
        }
    ).supported_resource_operations() == frozenset({ResourceOperationKind.RENAME})
