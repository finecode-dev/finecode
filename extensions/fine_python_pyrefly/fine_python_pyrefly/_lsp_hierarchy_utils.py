"""Conversion utilities between FineCode hierarchy types and raw LSP dicts.

Shared by all 6 hierarchy handlers to avoid duplication of field-mapping logic.
Field mapping notes:
  - selection_range (FineCode) <-> selectionRange (LSP)
  - uri: ResourceUri (FineCode) <-> "uri": str (LSP)
  - kind: SymbolKind IntEnum <-> "kind": int (LSP)
  - tags: list[SymbolTag] | None <-> "tags": list[int] (LSP, omitted when empty/None)
"""
from __future__ import annotations

from typing import Any

from finecode_extension_api import common_types
from fine_code_hierarchy.types import SymbolKind, SymbolTag
from fine_code_hierarchy.call_hierarchy_incoming_calls_action import (
    CallHierarchyIncomingCall,
)
from fine_code_hierarchy.call_hierarchy_outgoing_calls_action import (
    CallHierarchyOutgoingCall,
)
from fine_code_hierarchy.text_document_prepare_call_hierarchy_action import (
    CallHierarchyItem,
)
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    TypeHierarchyItem,
)
from finecode_extension_api.resource_uri import ResourceUri


# ---------------------------------------------------------------------------
# Range / Position helpers
# ---------------------------------------------------------------------------


def _range_to_lsp(r: common_types.Range) -> dict[str, Any]:
    return {
        "start": {"line": r.start.line, "character": r.start.character},
        "end": {"line": r.end.line, "character": r.end.character},
    }


def _range_from_lsp(d: dict[str, Any]) -> common_types.Range:
    start = d.get("start", {})
    end = d.get("end", {})
    return common_types.Range(
        start=common_types.Position(
            line=start.get("line", 0),
            character=start.get("character", 0),
        ),
        end=common_types.Position(
            line=end.get("line", 0),
            character=end.get("character", 0),
        ),
    )


# ---------------------------------------------------------------------------
# CallHierarchyItem
# ---------------------------------------------------------------------------


def call_hierarchy_item_to_lsp(item: CallHierarchyItem) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": item.name,
        "kind": int(item.kind),
        "uri": str(item.uri),
        "range": _range_to_lsp(item.range),
        "selectionRange": _range_to_lsp(item.selection_range),
    }
    if item.detail is not None:
        d["detail"] = item.detail
    if item.tags:
        d["tags"] = [int(t) for t in item.tags]
    return d


def call_hierarchy_item_from_lsp(d: dict[str, Any]) -> CallHierarchyItem:
    tags_raw: list[int] | None = d.get("tags")
    tags = [SymbolTag(t) for t in tags_raw] if tags_raw else None
    return CallHierarchyItem(
        name=d.get("name", ""),
        kind=SymbolKind(d.get("kind", 1)),
        uri=ResourceUri(d.get("uri", "")),
        range=_range_from_lsp(d.get("range", {})),
        selection_range=_range_from_lsp(d.get("selectionRange", {})),
        detail=d.get("detail"),
        tags=tags,
    )


# ---------------------------------------------------------------------------
# TypeHierarchyItem
# ---------------------------------------------------------------------------


def type_hierarchy_item_to_lsp(item: TypeHierarchyItem) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": item.name,
        "kind": int(item.kind),
        "uri": str(item.uri),
        "range": _range_to_lsp(item.range),
        "selectionRange": _range_to_lsp(item.selection_range),
    }
    if item.detail is not None:
        d["detail"] = item.detail
    if item.tags:
        d["tags"] = [int(t) for t in item.tags]
    return d


def type_hierarchy_item_from_lsp(d: dict[str, Any]) -> TypeHierarchyItem:
    tags_raw: list[int] | None = d.get("tags")
    tags = [SymbolTag(t) for t in tags_raw] if tags_raw else None
    return TypeHierarchyItem(
        name=d.get("name", ""),
        kind=SymbolKind(d.get("kind", 1)),
        uri=ResourceUri(d.get("uri", "")),
        range=_range_from_lsp(d.get("range", {})),
        selection_range=_range_from_lsp(d.get("selectionRange", {})),
        detail=d.get("detail"),
        tags=tags,
    )


# ---------------------------------------------------------------------------
# CallHierarchyIncomingCall / OutgoingCall
# ---------------------------------------------------------------------------


def incoming_call_from_lsp(d: dict[str, Any]) -> CallHierarchyIncomingCall:
    """Convert LSP {from: item, fromRanges: [...]} to FineCode CallHierarchyIncomingCall."""
    return CallHierarchyIncomingCall(
        caller=call_hierarchy_item_from_lsp(d.get("from", {})),
        call_ranges=[_range_from_lsp(r) for r in d.get("fromRanges", [])],
    )


def outgoing_call_from_lsp(d: dict[str, Any]) -> CallHierarchyOutgoingCall:
    """Convert LSP {to: item, fromRanges: [...]} to FineCode CallHierarchyOutgoingCall."""
    return CallHierarchyOutgoingCall(
        callee=call_hierarchy_item_from_lsp(d.get("to", {})),
        call_ranges=[_range_from_lsp(r) for r in d.get("fromRanges", [])],
    )
