"""Expansion of resource locations in a CLI action payload.

A payload is forwarded verbatim to every ER taking part in a run, and an ER runs
in its own project directory.  Anything in it that names a file relative to a
directory therefore means a different file in each ER that reads it — the CLI is
the last process that knows the directory the user typed it in, so resource
fields are made absolute here and only absolute ones go over the wire.

Which fields those are is not guessed.  Each action's payload schema
(``actions/getPayloadSchemas``) marks its ``ResourceUri`` fields with
``format: "uri"``, and only fields the schema vouches for are rewritten.  That is
what makes it safe to accept a plain path such as ``./pkg`` as a resource: on an
unmarked field the same string is just a string, and nothing here will touch it.
"""

from __future__ import annotations

import pathlib
import typing

from finecode_extension_api.resource_uri import (
    is_relative_file_uri,
    resource_location_to_uri,
)

_URI_FORMAT = "uri"


def merge_payload_properties(
    schemas: dict[str, dict | None],
) -> dict[str, dict]:
    """Combine the per-action field schemas into one field → schema map.

    One CLI invocation can name several actions, and they all receive the same
    payload, so the fields have to be judged collectively.  When two actions
    describe a field differently the resource reading wins: the value has to be
    a usable resource for the action that asks for one, and an action that only
    wants a string still accepts the absolute URI it becomes.
    """
    merged: dict[str, dict] = {}
    for schema in schemas.values():
        if not schema:
            continue
        for field_name, field_schema in (schema.get("properties") or {}).items():
            existing = merged.get(field_name)
            if existing is None or (
                not _describes_resource(existing) and _describes_resource(field_schema)
            ):
                merged[field_name] = field_schema
    return merged


def absolutize_payload(
    payload: dict[str, typing.Any],
    properties: dict[str, dict],
    base_dir: pathlib.Path,
) -> dict[str, typing.Any]:
    """Return *payload* with every schema-confirmed resource made absolute.

    Fields absent from *properties* are passed through untouched — an unknown
    field is not a field known to hold a resource.
    """
    return {
        key: _absolutize(value, properties.get(key), base_dir)
        for key, value in payload.items()
    }


def find_unresolved_relative_uris(
    payload: dict[str, typing.Any],
) -> list[str]:
    """Locate relative ``file://`` URIs still left in *payload*.

    Run after :func:`absolutize_payload`, this reports what the schema could not
    vouch for.  Anything named here would otherwise be resolved separately, and
    differently, by each ER that received it.  Values are reported as
    ``field[0].sub`` paths so the caller can say where to look.
    """
    found: list[str] = []
    for key, value in payload.items():
        _collect_relative_uris(value, key, found)
    return found


def _describes_resource(field_schema: dict | None) -> bool:
    return bool(field_schema) and field_schema.get("format") == _URI_FORMAT


def _absolutize(
    value: typing.Any, field_schema: dict | None, base_dir: pathlib.Path
) -> typing.Any:
    if not field_schema:
        return value

    if _describes_resource(field_schema):
        # A resource field holds a string; anything else is the caller's error
        # to hear about from the action, not something to rewrite on the way.
        return (
            resource_location_to_uri(value, base_dir)
            if isinstance(value, str)
            else value
        )

    schema_type = field_schema.get("type")
    if schema_type == "array" and isinstance(value, list):
        item_schema = field_schema.get("items")
        return [_absolutize(item, item_schema, base_dir) for item in value]
    if schema_type == "object" and isinstance(value, dict):
        sub_properties = field_schema.get("properties") or {}
        return {
            key: _absolutize(sub_value, sub_properties.get(key), base_dir)
            for key, sub_value in value.items()
        }
    return value


def _collect_relative_uris(value: typing.Any, path: str, found: list[str]) -> None:
    if isinstance(value, str):
        if is_relative_file_uri(value):
            found.append(f"{path} = {value}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_relative_uris(item, f"{path}[{index}]", found)
    elif isinstance(value, dict):
        for key, sub_value in value.items():
            _collect_relative_uris(sub_value, f"{path}.{key}", found)
