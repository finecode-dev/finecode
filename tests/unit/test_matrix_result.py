"""Tests for finecode.wm_server.matrix_result.VariantKeyedRunResult.

Assumption note: Interpreter is treated as a frozen dataclass constructed via
keyword arguments `implementation` and `version` (e.g.
`Interpreter(implementation="cpython", version="3.11")`), whose `.canonical`
property renders as `"cpython@3.11"`, per the plan's own example.
"""

import dataclasses

from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.matrix_result import VariantKeyedRunResult
from finecode_extension_api import textstyler
from finecode_extension_api.code_action import RunActionResult, RunReturnCode


@dataclasses.dataclass
class StubResult(RunActionResult):
    label: str
    code: RunReturnCode = RunReturnCode.SUCCESS
    merged: list[str] = dataclasses.field(default_factory=list)

    @property
    def return_code(self) -> RunReturnCode:
        return self.code

    def to_text(self) -> str:
        return f"<{self.label}>"

    def update(self, other: RunActionResult) -> None:
        label = other.label if isinstance(other, StubResult) else repr(other)
        self.merged.append(label)


@dataclasses.dataclass
class NonVariantResult(RunActionResult):
    code: RunReturnCode = RunReturnCode.SUCCESS

    @property
    def return_code(self) -> RunReturnCode:
        return self.code

    def to_text(self) -> str:
        return "<non-variant>"

    def update(self, other: RunActionResult) -> None:
        pass


def plain(styled: str | textstyler.StyledText) -> str:
    if isinstance(styled, str):
        return styled
    out: list[str] = []
    for part in styled.text_parts:
        out.append(part if isinstance(part, str) else part["text"])
    return "".join(out)


INTERP_A = Interpreter(implementation="cpython", version="3.11")
INTERP_B = Interpreter(implementation="cpython", version="3.12")


def test_return_code_success_when_variants_empty() -> None:
    """A matrix run with no interpreters reports overall success rather than an unexplained failure."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(variants={})
    assert result.return_code == RunReturnCode.SUCCESS


def test_return_code_success_when_all_variants_succeed() -> None:
    """A matrix run is only reported successful when every interpreter variant actually succeeded."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={
            INTERP_A: StubResult(label="a", code=RunReturnCode.SUCCESS),
            INTERP_B: StubResult(label="b", code=RunReturnCode.SUCCESS),
        }
    )
    assert result.return_code == RunReturnCode.SUCCESS


def test_return_code_error_when_one_variant_errors_among_successes() -> None:
    """A single failing interpreter must surface as an overall failure so a user does not miss a broken variant."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={
            INTERP_A: StubResult(label="a", code=RunReturnCode.ERROR),
            INTERP_B: StubResult(label="b", code=RunReturnCode.SUCCESS),
        }
    )
    assert result.return_code == RunReturnCode.ERROR


def test_return_code_error_when_all_variants_error() -> None:
    """A matrix run where every interpreter failed must be reported as a failure, not silently swallowed."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={
            INTERP_A: StubResult(label="a", code=RunReturnCode.ERROR),
            INTERP_B: StubResult(label="b", code=RunReturnCode.ERROR),
        }
    )
    assert result.return_code == RunReturnCode.ERROR


def test_to_text_includes_both_interpreter_headers_and_variant_content_in_order() -> None:
    """A rendered matrix result must let a user see which interpreter each variant's output belongs to, in the order the variants ran."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={
            INTERP_A: StubResult(label="a-output"),
            INTERP_B: StubResult(label="b-output"),
        }
    )
    rendered = plain(result.to_text())
    assert "cpython@3.11" in rendered
    assert "cpython@3.12" in rendered
    assert "<a-output>" in rendered
    assert "<b-output>" in rendered
    assert rendered.index("cpython@3.11") < rendered.index("cpython@3.12")


def test_to_text_empty_variants_reports_no_interpreters_ran() -> None:
    """When no interpreter variants exist, the rendered result must say so explicitly instead of showing a blank/empty report."""
    result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(variants={})
    rendered = plain(result.to_text())
    assert rendered != ""
    assert "No interpreters" in rendered


def test_update_with_disjoint_keys_keeps_both_variants() -> None:
    """Merging results from a run covering a different interpreter must add that interpreter's result rather than replacing what's already there."""
    self_result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={INTERP_A: StubResult(label="a1")}
    )
    other_result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={INTERP_B: StubResult(label="b1")}
    )
    self_result.update(other_result)
    assert set(self_result.variants.keys()) == {INTERP_A, INTERP_B}
    assert self_result.variants[INTERP_A].label == "a1"
    assert self_result.variants[INTERP_B].label == "b1"


def test_update_with_matching_key_merges_into_existing_variant_instead_of_overwriting() -> None:
    """Re-running the same interpreter must combine with its prior result (e.g. accumulate test counts) instead of discarding the earlier run's data."""
    existing = StubResult(label="a1")
    self_result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={INTERP_A: existing}
    )
    other_result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={INTERP_A: StubResult(label="a2")}
    )
    self_result.update(other_result)
    assert set(self_result.variants.keys()) == {INTERP_A}
    assert self_result.variants[INTERP_A] is existing
    assert existing.merged == ["a2"]


def test_update_with_non_variant_result_is_noop() -> None:
    """Merging with a result of an unrelated shape must leave existing per-interpreter data untouched rather than corrupting or clearing it."""
    existing = StubResult(label="a1")
    self_result: VariantKeyedRunResult[StubResult] = VariantKeyedRunResult(
        variants={INTERP_A: existing}
    )
    other_result = NonVariantResult()
    self_result.update(other_result)
    assert set(self_result.variants.keys()) == {INTERP_A}
    assert self_result.variants[INTERP_A] is existing
    assert existing.merged == []
