"""Rendering of the trailing ``unhandled:`` block.

Misses are O(unmatched inputs) and now travel to the top-level caller, so the
rendered block must be grouped, counted, and capped — an unbounded block on a
whole-workspace run floods the default output and gets tuned out, which kills
the signal as surely as losing it. These tests pin the bound and the omission
on the clean path.
"""

from __future__ import annotations

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri

from finecode_extension_runner._services.run_action import (
    action_result_to_run_action_response,
)
from finecode_extension_runner.coverage_sink import render_unhandled_block


class _StyledResult(code_action.RunActionResult):
    """to_text() returns StyledText, like DiagnosticFilesRunResult does —
    exercising the append-to-StyledText branch of the render, not the
    plain-string one."""

    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        text.append("body line\n")
        return text


def _entry(status: CoverageStatus, name: str) -> ItemCoverage:
    return ItemCoverage(status=status, item=ResourceUri(f"file:///{name}"))


def test_clean_result_renders_nothing() -> None:
    """A run with no live misses must add no block at all — the mechanism
    exists to distinguish "unhandled" from "ran and found nothing"; printing
    an empty header on every clean run would blur that."""
    assert render_unhandled_block([]) == ""


def test_miss_is_named_with_its_reason() -> None:
    block = render_unhandled_block(
        [_entry(CoverageStatus.NO_SUBACTIONS, "input.py")]
    )
    assert block.startswith("unhandled:\n")
    assert "no_subactions (1):" in block
    assert "file:///input.py" in block


def test_block_is_grouped_by_reason_with_counts() -> None:
    block = render_unhandled_block(
        [
            _entry(CoverageStatus.NO_SUBACTIONS, "a.py"),
            _entry(CoverageStatus.NO_SUBACTIONS, "b.py"),
            _entry(CoverageStatus.NO_LANGUAGE_DETECTED, "c.toml"),
        ]
    )
    assert "  no_subactions (2):" in block
    assert "  no_language_detected (1):" in block


def test_long_miss_list_is_capped() -> None:
    """The per-item enumeration is bounded; the overflow is reported as a
    count so the operator still knows the scale."""
    block = render_unhandled_block(
        [_entry(CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, f"f{i}.toml") for i in range(25)]
    )
    assert "no_subaction_for_language (25):" in block
    assert "... and 15 more" in block
    # 10 rendered items, not 25
    assert block.count("    file:///f") == 10


def test_response_render_appends_block_after_styled_text() -> None:
    """The final-result render must append the block when ``to_text()`` returns
    StyledText — the shape every DiagnosticFilesRunResult subclass (lint,
    inspect_code, type_check) uses. Missing this branch would make the whole
    mechanism invisible at the CLI for those actions."""
    result = _StyledResult(
        coverage=[
            ItemCoverage(status=CoverageStatus.NO_LANGUAGE_DETECTED, item=ResourceUri("file:///x.md"))
        ]
    )
    response = action_result_to_run_action_response(result, ["string"])
    styled_json = response.result_by_format["styled_text_json"]
    parts = styled_json["parts"]
    assert "body line" in "".join(parts)
    assert any("unhandled:" in part for part in parts)
    assert any("file:///x.md" in part for part in parts)