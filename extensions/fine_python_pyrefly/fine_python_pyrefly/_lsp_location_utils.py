"""Conversion utilities for LSP Location results.

Used by definition, type_definition, implementation, and references handlers.
"""
from __future__ import annotations

from typing import Any

from finecode_extension_api import common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_symbol_info.types import Location


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


def location_from_lsp(d: dict[str, Any]) -> Location:
    return Location(
        uri=ResourceUri(d["uri"]),
        range=_range_from_lsp(d["range"]),
    )


def locations_from_lsp(result: list[dict[str, Any]] | dict[str, Any] | None) -> list[Location]:
    """Normalise any LSP definition/typeDefinition/implementation/references result.

    Handles: null, single Location, Location[], LocationLink[].
    LocationLinks are converted using targetUri + targetSelectionRange.
    """
    if not result:
        return []
    if isinstance(result, dict):
        # Single Location object
        return [location_from_lsp(result)]
    locations: list[Location] = []
    for item in result:
        if "targetUri" in item:
            # LocationLink
            locations.append(Location(
                uri=ResourceUri(item["targetUri"]),
                range=_range_from_lsp(item.get("targetSelectionRange") or item["targetRange"]),
            ))
        else:
            locations.append(location_from_lsp(item))
    return locations
