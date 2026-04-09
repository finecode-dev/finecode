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

from finecode_extension_api.resource_uri import ResourceUri


def extract_payload_schema(payload_cls: type) -> dict:
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
    properties: dict[str, dict] = {}
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


def _type_to_schema(t: type) -> dict:
    """Convert a single Python type annotation to a JSON Schema type object."""
    args = typing.get_args(t)

    # Union / Optional: T | None or typing.Optional[T]
    # Both forms produce args that include NoneType.
    if args and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])
        return {}

    origin = typing.get_origin(t)

    # list[T]
    if origin is list:
        item_schema = _type_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

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
        return {"type": "string", "format": "uri", "description": "A URI identifying a resource. For local files, use a file:// URI, e.g. file:///home/user/foo.py"}

    return {}
