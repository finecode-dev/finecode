"""Utilities for extracting JSON Schema descriptions from RunActionPayload dataclasses.

Used by the ``actions/getPayloadSchemas`` ER command to report parameter schemas
to the WM so that MCP clients can present real tool parameters.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import pathlib
import textwrap
import typing
from typing import Literal, TypedDict

from finecode_extension_api.resource_uri import ResourceUri

# --- shared payload-schema vocabulary -------------------------------------
#
# Everything down to the next marker is shared between this package and
# `finecode` (the WM), which consumes these fragments over
# `actions/getPayloadSchemas` — see `finecode/cli_app/payload_uris.py` and
# `finecode/wm_client.py`. It lives here because this module produces the
# fragments, and `finecode` already depends on `finecode_extension_runner`
# (as it does for `logs`, `concurrency` and `wal`).
#
# TODO: this belongs in a package common to both, not in the ER. There is no
# such package today: `finecode_extension_api` is the extension authors'
# public API and this is internal plumbing, so it must not go there. Move the
# block wholesale when a common internal package exists.

JsonValue: typing.TypeAlias = (
    "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]"
)
"""One decoded JSON value.

Deliberately open: a payload field holds whatever the caller sent, and the type
is not knowable until it is narrowed by `isinstance` at the point of use. This
is the *honest* open type, not a stand-in for a shape nobody wrote down — the
shape of a schema fragment is `FieldSchema` below.
"""

SchemaType: typing.TypeAlias = Literal[
    "boolean", "integer", "number", "string", "array", "object", "null"
]
"""The `type` values `_type_to_schema` emits. `"null"` is produced only in
output mode (as one arm of an `anyOf`), never in a payload schema. A fragment
for an unmapped Python type carries no `type` key at all rather than a seventh
value."""


class FieldSchema(TypedDict, total=False):
    """A JSON Schema fragment describing one payload field.

    Every key is optional because an unmapped Python type produces `{}`. The key
    set is closed: it is exactly what `_type_to_schema` writes, and the mapping
    table in `extract_payload_schema` is its documentation.
    """

    type: SchemaType
    format: Literal["uri"]
    """Present only on `ResourceUri` fields. This is the marker the CLI uses to
    decide which fields it may rewrite to absolute URIs."""
    description: str
    enum: list[JsonValue]
    """Members of an `enum.Enum` field, as their `.value`s."""
    items: FieldSchema
    """Element schema of an `array` field."""
    properties: dict[str, FieldSchema]
    """Field schemas of an `object` (nested dataclass) field."""
    required: list[str]
    """Names of the `object` field's properties that have no default."""
    anyOf: list["FieldSchema"]
    """Output mode only; never present in a payload schema. A `T | None` field
    is described as `{"anyOf": [<T>, {"type": "null"}]}`."""
    additionalProperties: bool
    """Output mode only; never present in a payload schema. Always `False` on
    an output-mode object, because model output is structured strictly and a
    key the dataclass does not declare is an error, not a tolerated extra."""


class PayloadSchema(TypedDict):
    """The schema of a whole `RunActionPayload` subclass."""

    properties: dict[str, FieldSchema]
    required: list[str]


# --- end shared payload-schema vocabulary ---------------------------------


class OutputSchemaError(TypeError):
    """A dataclass field has no complete JSON Schema mapping in output mode.

    Output mode cannot fall back to `{}` the way payload mode does: an empty
    fragment tells the model nothing about the field, so a type that is not
    mapped is a broken schema rather than a permissive one.
    """


def extract_payload_schema(payload_cls: type) -> PayloadSchema:
    """Return a JSON Schema fragment describing the fields of a RunActionPayload subclass.

    The result has two keys:

    - ``properties``: mapping of field name → JSON Schema type object.
    - ``required``: list of field names that have no default value (both
      ``field.default`` and ``field.default_factory`` are ``dataclasses.MISSING``).

    Type mapping:

    ========================  =====================================================
    Python type               JSON Schema
    ========================  =====================================================
    ``bool``                  ``{"type": "boolean"}``
    ``str``                   ``{"type": "string"}``
    ``int``                   ``{"type": "integer"}``
    ``float``                 ``{"type": "number"}``
    ``pathlib.Path``          ``{"type": "string"}``
    ``ResourceUri``           ``{"type": "string", "format": "uri"}``
    ``enum.Enum`` subclass    ``{"type": "string", "enum": [<member values>]}``
    ``list[T]``               ``{"type": "array", "items": <schema for T>}``
    ``T | None``              same schema as ``T`` (optionality via ``required``)
    dataclass                 ``{"type": "object", "properties": {...}}`` (recursive)
    unknown                   ``{}``
    ========================  =====================================================

    Args:
        payload_cls: A ``RunActionPayload`` subclass decorated with
            ``@dataclasses.dataclass``.

    Returns:
        A dict with ``"properties"`` and ``"required"`` keys, suitable for
        embedding directly into an MCP ``Tool.inputSchema``.
    """
    try:
        hints = typing.get_type_hints(payload_cls)
    except Exception:
        hints = {}

    field_descriptions = _extract_field_descriptions(payload_cls)
    properties: dict[str, FieldSchema] = {}
    required: list[str] = []

    for field in dataclasses.fields(payload_cls):
        prop = _type_to_schema(hints.get(field.name, type(None)))
        desc = field_descriptions.get(field.name)
        if desc:
            prop["description"] = desc
        properties[field.name] = prop

        if (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING  # type: ignore[misc]
        ):
            required.append(field.name)

    return {"properties": properties, "required": required}


def extract_output_schema(cls: type) -> dict[str, typing.Any]:
    """Return a complete JSON Schema for a dataclass used as structured output.

    Unlike :func:`extract_payload_schema`, every field must have a mapping: the
    result carries `"additionalProperties": false` at every object level and a
    `T | None` field becomes an `anyOf` with an explicit `null` arm. Types with
    no mapping (`dict`, `Any`, `Literal`, a bare `list`, a union of two
    non-`None` types) raise :class:`OutputSchemaError`.
    """
    if not (dataclasses.is_dataclass(cls) and isinstance(cls, type)):
        raise TypeError(f"{cls!r} is not a dataclass")

    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}

    field_descriptions = _extract_field_descriptions(cls)
    properties: dict[str, FieldSchema] = {}
    required: list[str] = []

    for field in dataclasses.fields(cls):
        prop = _type_to_schema(
            hints.get(field.name, type(None)), output=True, path=f"$.{field.name}"
        )
        desc = field_descriptions.get(field.name)
        if desc:
            prop["description"] = desc
        properties[field.name] = prop

        if (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING  # type: ignore[misc]
        ):
            required.append(field.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _extract_field_descriptions(cls: type) -> dict[str, str]:
    """Extract attribute docstrings from a dataclass class body via AST.

    An attribute docstring is a bare string literal on the line immediately
    after an annotated assignment (``ast.AnnAssign``).  This is the pattern
    recognised by Sphinx autodoc and used throughout the FineCode action API.

    Returns an empty dict if source inspection fails (e.g. built-ins, .pyc-only
    installs) so callers always get a safe result.
    """
    try:
        source = inspect.getsource(cls)
        source = textwrap.dedent(source)
        tree = ast.parse(source)
    except Exception:
        return {}

    class_def = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)),
        None,
    )
    if class_def is None:
        return {}

    descriptions: dict[str, str] = {}
    body = class_def.body
    for i, stmt in enumerate(body):
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not isinstance(stmt.target, ast.Name):
            continue
        field_name = stmt.target.id
        if i + 1 < len(body):
            next_stmt = body[i + 1]
            if (
                isinstance(next_stmt, ast.Expr)
                and isinstance(next_stmt.value, ast.Constant)
                and isinstance(next_stmt.value.value, str)
            ):
                descriptions[field_name] = next_stmt.value.value.strip()

    return descriptions


def _type_to_schema(t: type, *, output: bool = False, path: str = "$") -> FieldSchema:
    """Convert a single Python type annotation to a JSON Schema type object.

    With ``output=False`` every branch returns exactly what it always has; all
    output-only behaviour sits behind ``if output:``.
    """
    args = typing.get_args(t)

    # Union / Optional: T | None or typing.Optional[T]
    # Both forms produce args that include NoneType.
    if args and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner = _type_to_schema(non_none[0], output=output, path=path)
            if output:
                return {"anyOf": [inner, {"type": "null"}]}
            return inner
        if output:
            raise OutputSchemaError(f"no JSON Schema mapping for {t!r} at {path}")
        return {}

    origin = typing.get_origin(t)

    # list[T]
    if origin is list:
        item_schema: FieldSchema = (
            _type_to_schema(args[0], output=output, path=f"{path}[]") if args else {}
        )
        return {"type": "array", "items": item_schema}

    # Bare `list` and `dict` have no mapping. They are rejected in output mode
    # by the fall-through at the bottom, which is also what handles `Any`.

    # Enum subclasses (check before str — StrEnum is also a str subclass)
    if isinstance(t, type) and issubclass(t, enum.Enum):
        return {"type": "string", "enum": [e.value for e in t]}

    # Primitives — bool before int (bool is a subclass of int)
    if t is bool:
        return {"type": "boolean"}
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if t is str:
        return {"type": "string"}
    if t is pathlib.Path:
        return {"type": "string"}
    if t is ResourceUri:
        return {
            "type": "string",
            "format": "uri",
            "description": "A URI identifying a resource. For local files, use a file:// URI, e.g. file:///home/user/foo.py",
        }

    # Nested dataclasses (e.g. Range, Position) — describe as a JSON object.
    if dataclasses.is_dataclass(t) and isinstance(t, type):
        try:
            sub_hints = typing.get_type_hints(t)
        except Exception:
            sub_hints = {}

        sub_descriptions = _extract_field_descriptions(t) if output else {}
        sub_properties: dict[str, FieldSchema] = {}
        sub_required: list[str] = []
        for sub_field in dataclasses.fields(t):
            sub_prop = _type_to_schema(
                sub_hints.get(sub_field.name, type(None)),
                output=output,
                path=f"{path}.{sub_field.name}",
            )
            if output:
                sub_desc = sub_descriptions.get(sub_field.name)
                if sub_desc:
                    sub_prop["description"] = sub_desc
            sub_properties[sub_field.name] = sub_prop
            if (
                sub_field.default is dataclasses.MISSING
                and sub_field.default_factory is dataclasses.MISSING  # type: ignore[misc]
            ):
                sub_required.append(sub_field.name)

        schema: FieldSchema = {"type": "object", "properties": sub_properties}
        if output:
            schema["additionalProperties"] = False
        if sub_required:
            schema["required"] = sub_required
        return schema

    if output:
        message = f"no JSON Schema mapping for {t!r} at {path}"
        if origin is typing.Literal:
            message += "; use an enum.Enum"
        raise OutputSchemaError(message)

    return {}
