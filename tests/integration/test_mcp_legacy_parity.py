"""Exact parity for the working legacy MCP server.

Compares fresh newline-stdio traces for both negotiated revisions against the
frozen reference, plus the transport method inventory from its separate channel.
Mutation controls prove the comparator notices each class of change.
"""

from __future__ import annotations

import copy
import json
import pathlib
import typing

from tests.integration import mcp_legacy_support


def _reference_path() -> pathlib.Path:
    return (
        pathlib.Path(__file__).resolve().parents[2]
        / "tests"
        / "integration"
        / "fixtures"
        / "mcp_legacy_before.json"
    )


def _load_reference() -> mcp_legacy_support.LegacyTraceFixture:
    path = _reference_path()
    assert path.exists(), f"missing frozen reference: {path}"
    return mcp_legacy_support.load_reference(path)


def _find_by_id(
    trace: list[mcp_legacy_support.JsonValue], request_id: int
) -> tuple[int, dict[str, mcp_legacy_support.JsonValue]]:
    for index, message in enumerate(trace):
        assert isinstance(message, dict)
        typed = typing.cast(dict[str, mcp_legacy_support.JsonValue], message)
        if typed.get("id") == request_id:
            return index, typed
    raise AssertionError(f"id {request_id} not in trace")


def _mutate(
    base: mcp_legacy_support.LegacyTraceFixture, kind: str
) -> mcp_legacy_support.LegacyTraceFixture:
    mutated = copy.deepcopy(base)
    version = "2025-06-18"
    trace = mutated["traces"][version]
    if kind == "schema":
        _, listed = _find_by_id(trace, 3)
        result = typing.cast(dict[str, mcp_legacy_support.JsonValue], listed["result"])
        tools = typing.cast(list[mcp_legacy_support.JsonValue], result["tools"])
        assert isinstance(tools, list) and tools
        first = typing.cast(dict[str, mcp_legacy_support.JsonValue], tools[0])
        first = dict(first)
        first["description"] = str(first.get("description", "")) + " (mutated)"
        tools[0] = first
    elif kind == "negotiated_version":
        _, initialized = _find_by_id(trace, 1)
        init_result = typing.cast(
            dict[str, mcp_legacy_support.JsonValue], initialized["result"]
        )
        init_result["protocolVersion"] = "1999-01-01"
    elif kind == "ping":
        _, pinged = _find_by_id(trace, 2)
        pinged["result"] = {"ok": True}
    elif kind == "question_id":
        for message in trace:
            assert isinstance(message, dict)
            if message.get("method") == "elicitation/create":
                message["id"] = 9999
                break
        else:
            raise AssertionError("no elicitation/create in trace")
    elif kind == "notification_order":
        first_message = next(
            index
            for index, message in enumerate(trace)
            if isinstance(message, dict)
            and message.get("method") == "notifications/message"
        )
        second_message = next(
            index
            for index, message in enumerate(trace)
            if isinstance(message, dict)
            and message.get("method") == "notifications/tools/list_changed"
        )
        trace[first_message], trace[second_message] = (
            trace[second_message],
            trace[first_message],
        )
    else:
        raise AssertionError(f"unknown control {kind}")
    return mutated


def test_legacy_parity_matches_frozen_reference() -> None:
    """Fresh stdio traces for both revisions match the frozen before fixture."""
    reference = _load_reference()
    assert sorted(reference["traces"].keys()) == ["2025-06-18", "2025-11-25"]
    for version, trace in reference["traces"].items():
        assert trace, f"empty trace for {version}"
    assert reference["methods"]["requests"] == [
        "initialize",
        "ping",
        "tools/call",
        "tools/list",
    ]
    assert reference["methods"]["notifications"] == [
        "$/cancelRequest",
        "notifications/initialized",
    ]

    current = mcp_legacy_support.normalize_fixture(
        mcp_legacy_support.collect_current_fixture()
    )
    diffs = mcp_legacy_support.compare_fixtures(current, reference)
    assert diffs == [], "legacy parity drifted:\n" + "\n".join(diffs)


def test_comparator_rejects_schema_change() -> None:
    """A tool description change must fail the comparison."""
    reference = _load_reference()
    mutated = _mutate(reference, "schema")
    assert mcp_legacy_support.compare_fixtures(mutated, reference) != []


def test_comparator_rejects_negotiated_version_change() -> None:
    """A negotiated version change must fail the comparison."""
    reference = _load_reference()
    mutated = _mutate(reference, "negotiated_version")
    assert mcp_legacy_support.compare_fixtures(mutated, reference) != []


def test_comparator_rejects_ping_change() -> None:
    """A ping response change must fail the comparison."""
    reference = _load_reference()
    mutated = _mutate(reference, "ping")
    assert mcp_legacy_support.compare_fixtures(mutated, reference) != []


def test_comparator_rejects_question_id_change() -> None:
    """Breaking the elicitation request identity must fail the comparison."""
    reference = _load_reference()
    mutated = _mutate(reference, "question_id")
    assert mcp_legacy_support.compare_fixtures(mutated, reference) != []


def test_comparator_rejects_notification_order_change() -> None:
    """Reordering side-channel values must fail the comparison."""
    reference = _load_reference()
    mutated = _mutate(reference, "notification_order")
    assert mcp_legacy_support.compare_fixtures(mutated, reference) != []


def test_comparator_control_kinds_are_complete() -> None:
    """Every required mutation control ran."""
    assert mcp_legacy_support.REQUESTED_VERSIONS == ("2025-06-18", "2025-11-25")
    assert mcp_legacy_support.NEGOTIATED_VERSION == "2025-06-18"
    reference = _load_reference()
    for kind in (
        "schema",
        "negotiated_version",
        "ping",
        "question_id",
        "notification_order",
    ):
        mutated = _mutate(reference, kind)
        assert mcp_legacy_support.compare_fixtures(mutated, reference) != [], kind
    current = json.loads(_reference_path().read_text(encoding="utf-8"))
    assert set(current["traces"].keys()) == {"2025-06-18", "2025-11-25"}
