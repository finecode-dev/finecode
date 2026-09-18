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

import ast
import json
import pathlib

from finecode_extension_api.resource_uri import (
    is_relative_file_uri,
    resource_location_to_uri,
)

# The schema vocabulary is defined next to the code that produces the fragments.
# It is shared plumbing with no common package to live in yet — see the marked
# block in `finecode_extension_runner.schema_utils`.
from finecode_extension_runner.schema_utils import (
    FieldSchema,
    JsonValue,
    PayloadSchema,
)

_URI_FORMAT = "uri"


def merge_payload_properties(
    schemas: dict[str, PayloadSchema | None],
) -> dict[str, FieldSchema]:
    """Combine the per-action field schemas into one field → schema map.

    One CLI invocation can name several actions, and they all receive the same
    payload, so the fields have to be judged collectively.  When two actions
    describe a field differently the resource reading wins: the value has to be
    a usable resource for the action that asks for one, and an action that only
    wants a string still accepts the absolute URI it becomes.
    """
    merged: dict[str, FieldSchema] = {}
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


def coerce_raw_value(
    raw: str, field_schema: FieldSchema, fallback: JsonValue
) -> JsonValue:
    """Parse one raw ``--field=value`` string guided by the field's schema.

    A value whose type JSON parsing could not settle (``"1.0"`` as a number or
    a string) is resolved by the declared type: a ``string`` field keeps the
    text the user typed, a ``number`` field parses it.  Where the schema
    vouches for nothing — an empty fragment, or one whose type this function
    does not coerce — *fallback* is returned; it is the caller's blind parse of
    the same string.

    Raises:
        ValueError: the raw text cannot be the declared type, with a message
            that names the type and, for lists, shows the expected form.
    """
    if not field_schema:
        return fallback

    schema_type = field_schema.get("type")

    if schema_type == "string":
        if "enum" in field_schema:
            valid = field_schema["enum"]
            if raw not in valid:
                raise ValueError(
                    f"expected one of {', '.join(repr(value) for value in valid)}"
                )
        return raw

    if schema_type == "boolean":
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ValueError("expected a boolean ('true' or 'false')")

    if schema_type == "integer":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"expected an integer, got {raw!r}") from None

    if schema_type == "number":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"expected a number, got {raw!r}") from None

    if schema_type == "array":
        parsed = _parse_structured(raw)
        if not isinstance(parsed, list):
            raise ValueError(f"expected a list, got {raw!r}; use e.g. ['a', 'b']")
        return parsed

    if schema_type == "object":
        parsed = _parse_structured(raw)
        if not isinstance(parsed, dict):
            raise ValueError(
                f"expected an object, got {raw!r}; use e.g. {{'key': 'value'}}"
            )
        return parsed

    return fallback


def _parse_structured(raw: str) -> JsonValue:
    """Return *raw* parsed as JSON, then as a Python literal, else unchanged."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw


def absolutize_payload(
    payload: dict[str, JsonValue],
    properties: dict[str, FieldSchema],
    base_dir: pathlib.Path,
) -> dict[str, JsonValue]:
    """Return *payload* with every schema-confirmed resource made absolute.

    Fields absent from *properties* are passed through untouched — an unknown
    field is not a field known to hold a resource.
    """
    return {
        key: _absolutize(value, properties.get(key), base_dir)
        for key, value in payload.items()
    }


def find_unresolved_relative_uris(
    payload: dict[str, JsonValue],
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


def _describes_resource(field_schema: FieldSchema | None) -> bool:
    return bool(field_schema) and field_schema.get("format") == _URI_FORMAT


def _absolutize(
    value: JsonValue, field_schema: FieldSchema | None, base_dir: pathlib.Path
) -> JsonValue:
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


def _collect_relative_uris(value: JsonValue, path: str, found: list[str]) -> None:
    if isinstance(value, str):
        if is_relative_file_uri(value):
            found.append(f"{path} = {value}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_relative_uris(item, f"{path}[{index}]", found)
    elif isinstance(value, dict):
        for key, sub_value in value.items():
            _collect_relative_uris(sub_value, f"{path}.{key}", found)
