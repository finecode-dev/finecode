"""Tests for the backend-independent structured-output helpers.

Every backend reports an unknown profile and extracts a fenced JSON block the
same way, so the wording and the extraction rule are pinned here rather than in
each backend's suite.
"""

from __future__ import annotations

import json

import pytest

from fine_agent import backend_support


def test_unknown_profile_error_sorts_names() -> None:
    assert backend_support.unknown_profile_error("nope", ["b", "a"]) == (
        "unknown agent profile 'nope'; configured profiles: a, b"
    )


def test_unknown_profile_error_says_none_when_empty() -> None:
    assert backend_support.unknown_profile_error("nope", []) == (
        "unknown agent profile 'nope'; configured profiles: none"
    )


def test_json_output_instruction_embeds_the_indented_schema() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    text = backend_support.json_output_instruction(schema)

    assert json.dumps(schema, indent=2) in text
    assert text.rstrip().endswith("```")


def test_extract_last_json_block_takes_the_final_block() -> None:
    text = (
        "first\n```json\n{\"n\": 1}\n```\n"
        "then\n```json\n{\"n\": 2}\n```\n"
    )

    assert backend_support.extract_last_json_block(text) == {"n": 2}


def test_extract_last_json_block_raises_missing() -> None:
    with pytest.raises(backend_support.MissingJsonBlock) as exc_info:
        backend_support.extract_last_json_block("no block here")

    assert str(exc_info.value) == "no fenced json block"


def test_extract_last_json_block_raises_invalid() -> None:
    with pytest.raises(backend_support.InvalidJsonBlock) as exc_info:
        backend_support.extract_last_json_block("```json\n{not json}\n```")

    assert str(exc_info.value).startswith("invalid JSON in the final json block:")


def test_structured_output_errors_are_value_errors() -> None:
    """Backends catch them as a family, and a `ValueError` keeps them out of the
    "unexpected exception" path."""
    assert issubclass(backend_support.MissingJsonBlock, ValueError)
    assert issubclass(backend_support.InvalidJsonBlock, ValueError)
