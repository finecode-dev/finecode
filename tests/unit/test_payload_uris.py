"""Tests for making a CLI payload's resources absolute before it is sent.

A payload reaches every ER taking part in a run unchanged, and an ER runs in its
own project directory.  Anything in it that names a file relative to a directory
therefore means a different file in each ER that reads it — the reason
``--project-paths="['file://./pkg']"`` reached one project as ``<ws>/pkg`` and
the next as ``<ws>/pkg/pkg``.

Which fields may be rewritten comes from the action's payload schema, never from
the shape of the value.  That distinction is what these tests pin down: the same
string is a resource in a field the schema marks and plain text in one it does
not.
"""

from __future__ import annotations

import pathlib

import pytest
from finecode_extension_runner.schema_utils import FieldSchema

from finecode.cli_app.payload_uris import (
    absolutize_payload,
    coerce_raw_value,
    find_unresolved_relative_uris,
    merge_payload_properties,
)

_WS = pathlib.Path("/ws")
_URI: FieldSchema = {"type": "string", "format": "uri"}
_STRING: FieldSchema = {"type": "string"}

# Stands in for the caller's blind JSON parse of the same raw string. Every
# `coerce_raw_value` test that does not exercise the fallback passes a value that
# could not be confused with a real result.
_UNUSED_FALLBACK = object()


def _properties(**fields: FieldSchema) -> dict[str, FieldSchema]:
    return dict(fields)


def test_relative_uri_in_a_marked_field_is_expanded() -> None:
    payload = absolutize_payload(
        {"project_paths": ["file://./pkg"]},
        _properties(project_paths={"type": "array", "items": _URI}),
        _WS,
    )

    assert payload == {"project_paths": ["file:///ws/pkg"]}


def test_plain_path_in_a_marked_field_becomes_a_uri() -> None:
    """The schema is what makes this safe: the field is declared a resource, so
    a bare path is a way of naming one rather than an ordinary string."""
    payload = absolutize_payload(
        {"project_paths": ["./pkg", "pkg/mod.py", "/elsewhere/mod.py"]},
        _properties(project_paths={"type": "array", "items": _URI}),
        _WS,
    )

    assert payload == {
        "project_paths": [
            "file:///ws/pkg",
            "file:///ws/pkg/mod.py",
            "file:///elsewhere/mod.py",
        ]
    }


def test_an_unmarked_field_is_never_rewritten() -> None:
    """The same strings that are resources above are just text here."""
    payload = absolutize_payload(
        {"message": "./pkg", "pattern": "file://./pkg"},
        _properties(message=_STRING, pattern=_STRING),
        _WS,
    )

    assert payload == {"message": "./pkg", "pattern": "file://./pkg"}


def test_a_field_absent_from_the_schema_is_never_rewritten() -> None:
    payload = absolutize_payload({"unknown": "./pkg"}, _properties(), _WS)

    assert payload == {"unknown": "./pkg"}


def test_absolute_uris_and_other_schemes_pass_through() -> None:
    payload = absolutize_payload(
        {"where": "file:///ws/pkg", "docs": "https://example.com/pkg"},
        _properties(where=_URI, docs=_URI),
        _WS,
    )

    assert payload == {"where": "file:///ws/pkg", "docs": "https://example.com/pkg"}


def test_resources_nested_in_an_object_field_are_expanded() -> None:
    payload = absolutize_payload(
        {"location": {"uri": "./pkg/mod.py", "line": 3}},
        _properties(
            location={
                "type": "object",
                "properties": {"uri": _URI, "line": {"type": "integer"}},
            }
        ),
        _WS,
    )

    assert payload == {"location": {"uri": "file:///ws/pkg/mod.py", "line": 3}}


def test_the_resource_reading_wins_when_actions_disagree() -> None:
    """One payload serves every action named in the run.  A value that has to be
    a usable resource for one of them is still an acceptable string for the rest."""
    properties = merge_payload_properties(
        {
            "a.PlainAction": {"properties": {"target": _STRING}, "required": []},
            "b.ResourceAction": {"properties": {"target": _URI}, "required": []},
        }
    )

    assert absolutize_payload({"target": "./pkg"}, properties, _WS) == {
        "target": "file:///ws/pkg"
    }


def test_actions_without_a_schema_contribute_nothing() -> None:
    properties = merge_payload_properties(
        {"a.Unimportable": None, "b.Known": {"properties": {"target": _URI}}}
    )

    assert properties == {"target": _URI}


def test_a_relative_uri_left_behind_is_reported_with_its_location() -> None:
    """What the caller refuses the run over: nothing vouched for this field, and
    sending it would have each ER resolve it against a different directory."""
    unresolved = find_unresolved_relative_uris(
        {
            "confirmed": "file:///ws/pkg",
            "unconfirmed": ["file:///ws/a", "file://./b"],
            "nested": {"deep": "file://./c"},
            "text": "just a string",
        }
    )

    assert unresolved == ["unconfirmed[1] = file://./b", "nested.deep = file://./c"]


def test_nothing_is_reported_once_every_resource_is_absolute() -> None:
    resolved = absolutize_payload(
        {"project_paths": ["file://./pkg"]},
        _properties(project_paths={"type": "array", "items": _URI}),
        _WS,
    )

    assert find_unresolved_relative_uris(resolved) == []


def test_a_string_field_keeps_a_numeric_looking_string() -> None:
    assert coerce_raw_value("1.0", _STRING, _UNUSED_FALLBACK) == "1.0"


def test_a_number_field_parses_a_numeric_looking_string() -> None:
    assert coerce_raw_value("1.0", {"type": "number"}, _UNUSED_FALLBACK) == 1.0


def test_an_integer_field_parses_and_rejects() -> None:
    assert coerce_raw_value("42", {"type": "integer"}, _UNUSED_FALLBACK) == 42
    with pytest.raises(ValueError, match="expected an integer"):
        coerce_raw_value("4.2", {"type": "integer"}, _UNUSED_FALLBACK)


def test_a_boolean_field_accepts_true_and_false_case_insensitively() -> None:
    assert coerce_raw_value("true", {"type": "boolean"}, _UNUSED_FALLBACK) is True
    assert coerce_raw_value("FALSE", {"type": "boolean"}, _UNUSED_FALLBACK) is False
    with pytest.raises(ValueError, match="expected a boolean"):
        coerce_raw_value("no", {"type": "boolean"}, _UNUSED_FALLBACK)


def test_an_array_field_parses_and_rejects_a_scalar() -> None:
    assert coerce_raw_value('["a", "b"]', {"type": "array"}, _UNUSED_FALLBACK) == ["a", "b"]
    with pytest.raises(ValueError, match="expected a list"):
        coerce_raw_value("file:///a", {"type": "array"}, _UNUSED_FALLBACK)


def test_an_object_field_parses_and_rejects_a_scalar() -> None:
    assert coerce_raw_value('{"a": 1}', {"type": "object"}, _UNUSED_FALLBACK) == {"a": 1}
    with pytest.raises(ValueError, match="expected an object"):
        coerce_raw_value("x", {"type": "object"}, _UNUSED_FALLBACK)


def test_an_enum_field_rejects_a_value_outside_the_list() -> None:
    schema = {"type": "string", "enum": ["A", "B"]}
    assert coerce_raw_value("B", schema, _UNUSED_FALLBACK) == "B"
    with pytest.raises(ValueError, match="expected one of"):
        coerce_raw_value("C", schema, _UNUSED_FALLBACK)


def test_an_empty_schema_returns_the_callers_blind_parse() -> None:
    """A schema that vouches for nothing hands the value back untouched, so the
    caller does not need a sentinel to find out that nothing was decided."""
    blind_parse = ["whatever", "the", "caller", "parsed"]

    assert coerce_raw_value("anything", {}, blind_parse) is blind_parse
