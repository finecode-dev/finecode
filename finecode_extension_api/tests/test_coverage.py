"""Coverage contract: "no handler covered this input" as typed data on results.

A dispatched action's caller must be able to tell "no handler covered this
input" apart from "a handler ran and found nothing" — the two have always
answered with the same empty value. This module pins the contract that
separates them: every ``RunActionResult`` carries a ``coverage`` list, merges
join it at the two framework choke points, and ``unhandled`` reads the live
misses.
"""

from __future__ import annotations

import dataclasses
import itertools
import json

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.code_action import (
    CoverageStatus,
    ItemCoverage,
    merge_coverage,
)
from finecode_extension_api.resource_uri import ResourceUri
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_runner._converter import converter

_ITEMS = [ResourceUri("file:///a.py"), ResourceUri("file:///b.toml")]
_MISS_STATUSES = [
    CoverageStatus.NO_SUBACTIONS,
    CoverageStatus.NO_LANGUAGE_DETECTED,
    CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
    CoverageStatus.ABSORBED,
]
_DETAILS = ["", "toml"]
_FULL_DOMAIN = list(itertools.product(_MISS_STATUSES, _ITEMS, _DETAILS))
_RANK = {
    CoverageStatus.NO_SUBACTIONS: 1,
    CoverageStatus.NO_LANGUAGE_DETECTED: 2,
    CoverageStatus.NO_SUBACTION_FOR_LANGUAGE: 3,
    CoverageStatus.ABSORBED: 4,
    CoverageStatus.HANDLED: 5,
}


def _cov(status: CoverageStatus, item: ResourceUri | None, detail: str = "") -> ItemCoverage:
    return ItemCoverage(status=status, item=item, detail=detail)


@dataclasses.dataclass
class _RequiredPositionalResult(code_action.RunActionResult):
    """Mirror of DiagnosticFilesRunResult's shape: a required positional field
    plus the type-mismatch-early-return ``update`` idiom."""

    messages: dict[ResourceUri, list[str]]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _RequiredPositionalResult):
            return
        for uri, new_messages in other.messages.items():
            self.messages.setdefault(uri, []).extend(new_messages)


@dataclasses.dataclass
class _EarlyReturnResult(code_action.RunActionResult):
    value: int = 0

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _EarlyReturnResult):
            return
        self.value += other.value


class _NoOwnUpdateResult(_EarlyReturnResult):
    """Inherits the wrapped ``update`` — must still join, exactly once."""


@dataclasses.dataclass
class _ParentResult(code_action.RunActionResult):
    messages: dict[ResourceUri, list[str]] = dataclasses.field(
        default_factory=dict
    )


@dataclasses.dataclass
class _ChildResult(_ParentResult):
    extra: str = ""


def _merge_pair(
    left: list[ItemCoverage], right: list[ItemCoverage]
) -> list[ItemCoverage]:
    """Join two coverage lists through the result-object ``update`` path."""
    result = _RequiredPositionalResult(messages={}, coverage=left)
    other = _RequiredPositionalResult(messages={}, coverage=right)
    result.update(other)
    return result.coverage


def test_bare_result_constructs_with_empty_coverage() -> None:
    """A bare ``RunActionResult`` exists (some actions still use it as their
    result type) and defaults to an empty coverage list — an empty answer is
    still a valid answer, just not an "unhandled" one."""
    result = code_action.RunActionResult()
    assert result.coverage == []


def test_required_positional_subclass_constructs_unchanged() -> None:
    """The kw-only defaulted ``coverage`` field must not disturb subclasses
    with required positional fields — ``DiagnosticFilesRunResult`` is the real
    one, and its ``messages`` must stay positionally constructible."""
    result = DiagnosticFilesRunResult(messages={})
    assert result.messages == {} and result.coverage == []
    positional = DiagnosticFilesRunResult({})
    assert positional.messages == {}
    with pytest.raises(TypeError):
        # coverage is keyword-only: a second positional argument is illegal
        DiagnosticFilesRunResult({}, [])  # type: ignore[call-arg]


def test_join_is_commutative_on_the_full_domain() -> None:
    """Every pair of coverage entries from the exhaustive product must merge
    identically in both orders — production merges iterate in arrival order,
    which is nondeterministic, so a diagnosis that depends on it would differ
    run to run.

    For a shared item the survivors collapse to exactly one entry: the
    higher-ranked status, ties on status resolved by the lexicographically
    minimum ``detail``.
    """
    for left_entry, right_entry in itertools.product(_FULL_DOMAIN, repeat=2):
        status_a, item_a, detail_a = left_entry
        status_b, item_b, detail_b = right_entry
        left = [_cov(status_a, item_a, detail_a)]
        right = [_cov(status_b, item_b, detail_b)]
        forward = _merge_pair(left, right)
        backward = _merge_pair(right, left)
        # The winning entry per item is deterministic; list order is
        # arrival order and not part of the diagnosis, so compare as sets.
        assert set(forward) == set(backward)
        if item_a != item_b:
            assert set(forward) == set(left + right)
            continue
        assert len(forward) == 1
        if status_a is status_b:
            assert forward[0].detail == min(detail_a, detail_b)
        else:
            higher = max([status_a, status_b], key=lambda s: _RANK[s])
            assert forward[0].status is higher


def test_join_is_idempotent_and_associative() -> None:
    """Merging a list with itself is a no-op, and merge order over three lists
    does not matter — dedupe only collapses duplicates, it never loses a
    distinct entry."""

    def entries_for(*tuples: tuple[CoverageStatus, ResourceUri, str]) -> list[ItemCoverage]:
        return [_cov(s, i, d) for s, i, d in tuples]

    for status, item, detail in _FULL_DOMAIN:
        single = entries_for((status, item, detail))
        assert merge_coverage(single, single) == single

    for triple in itertools.product(_FULL_DOMAIN, repeat=3):
        status_a, item_a, detail_a = triple[0]
        status_b, item_b, detail_b = triple[1]
        status_c, item_c, detail_c = triple[2]
        a = entries_for((status_a, item_a, detail_a))
        b = entries_for((status_b, item_b, detail_b))
        c = entries_for((status_c, item_c, detail_c))
        left = merge_coverage(merge_coverage(a, b), c)
        right = merge_coverage(a, merge_coverage(b, c))
        assert set(left) == set(right)


def test_handled_retracts_a_miss_regardless_of_order() -> None:
    """HANDLED exists so a catch-all handler can retract a sibling's miss; the
    retraction must work whichever side of the merge the retraction is on."""
    miss = [_cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0])]
    retraction = [_cov(CoverageStatus.HANDLED, _ITEMS[0])]
    assert _merge_pair(miss, retraction) == [retraction[0]]
    assert _merge_pair(retraction, miss) == [retraction[0]]
    result = _RequiredPositionalResult(messages={}, coverage=retraction)
    assert result.unhandled == []


def test_higher_ranked_miss_wins_in_both_orders() -> None:
    """Two different miss reasons for one item collapse to exactly one entry —
    the more-specific one — with a lone survivor either way the merge runs."""
    low = [_cov(CoverageStatus.NO_LANGUAGE_DETECTED, _ITEMS[0])]
    high = [_cov(CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, _ITEMS[0])]
    for left, right in ((low, high), (high, low)):
        merged = _merge_pair(left, right)
        assert len(merged) == 1
        assert merged[0].status is CoverageStatus.NO_SUBACTION_FOR_LANGUAGE


def test_serialization_round_trip_preserves_coverage() -> None:
    """Coverage travels with the value: asdict → json → structuring into the
    ``parent's RESULT_TYPE`` must keep the entries, with ``status`` a real
    enum member again (a child result's dict is structured into the parent's
    type at bridge boundaries, dropping child-only fields but never coverage)."""
    child = _ChildResult(
        messages={_ITEMS[0]: ["x"]},
        extra="child-only",
        coverage=[_cov(CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, _ITEMS[1], "toml")],
    )
    as_dict = dataclasses.asdict(child)
    payload = json.loads(json.dumps(as_dict))
    parent = converter.structure(payload, _ParentResult)
    assert parent.messages == {_ITEMS[0]: ["x"]}
    assert len(parent.coverage) == 1
    entry = parent.coverage[0]
    assert entry.status is CoverageStatus.NO_SUBACTION_FOR_LANGUAGE
    assert entry.item == _ITEMS[1]
    assert entry.detail == "toml"


def test_type_mismatch_early_return_still_joins_coverage() -> None:
    """The join runs before the author's ``update()`` body, so the uniform
    ``if not isinstance(other, X): return`` idiom can no longer discard a
    foreign result's coverage wholesale."""
    first = _EarlyReturnResult(value=1)
    first.coverage = [_cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0])]
    foreign = _RequiredPositionalResult(messages={})
    first.update(foreign)
    assert first.value == 1  # the author's early return still ran
    second = _EarlyReturnResult(
        value=2,
        coverage=[_cov(CoverageStatus.NO_LANGUAGE_DETECTED, _ITEMS[0])],
    )
    first.update(second)
    assert first.value == 3
    assert first.coverage == [second.coverage[0]]


def test_second_level_subclass_inherits_wrapped_update_and_joins_exactly_once() -> None:
    """A subclass that defines no ``update()`` of its own inherits the wrapped
    one and still joins — merging three results must leave exactly one entry
    per item, even when each side carried the same item with different
    details."""
    first = _NoOwnUpdateResult(
        value=10,
        coverage=[_cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0], "a")],
    )
    second = _NoOwnUpdateResult(
        value=20,
        coverage=[_cov(CoverageStatus.NO_LANGUAGE_DETECTED, _ITEMS[0], "b")],
    )
    third = _NoOwnUpdateResult(
        value=30,
        coverage=[_cov(CoverageStatus.NO_SUBACTION_FOR_LANGUAGE, _ITEMS[0], "c")],
    )
    first.update(second)
    first.update(third)
    assert first.value == 60
    assert len(first.coverage) == 1
    entry = first.coverage[0]
    assert entry.status is CoverageStatus.NO_SUBACTION_FOR_LANGUAGE
    assert entry.detail == "c"


@dataclasses.dataclass
class _LevelTwoResult(code_action.RunActionResult):
    child: code_action.RunActionResult | None = None


@dataclasses.dataclass
class _LevelOneResult(code_action.RunActionResult):
    children: list[code_action.RunActionResult] = dataclasses.field(
        default_factory=list
    )


def test_unhandled_sees_misses_nested_two_levels_deep() -> None:
    """A miss recorded on a result nested inside another result's field is
    visible from the outer result's ``unhandled`` — the pre-commit case, where
    a whole result object is nested inside ``action_results``."""
    innermost = _RequiredPositionalResult(
        messages={}, coverage=[_cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0])]
    )
    middle = _LevelTwoResult(child=innermost)
    outer = _LevelOneResult(children=[middle])
    assert [e.item for e in outer.unhandled] == [_ITEMS[0]]


def test_unhandled_predicate_ranks_below_absorbed() -> None:
    """Suppression is a property of the read, not just of the sink: an
    ABSORBED entry defeats a deeper miss wherever the two meet, and other
    items in the same tree are still reported."""
    absorbed = _LevelTwoResult(
        child=_RequiredPositionalResult(
            messages={},
            coverage=[_cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0])],
        ),
        coverage=[_cov(CoverageStatus.ABSORBED, _ITEMS[0])],
    )
    assert absorbed.unhandled == []
    absorbed.child = _RequiredPositionalResult(
        messages={},
        coverage=[
            _cov(CoverageStatus.NO_SUBACTIONS, _ITEMS[0]),
            _cov(CoverageStatus.NO_LANGUAGE_DETECTED, _ITEMS[1]),
        ],
    )
    assert [e.item for e in absorbed.unhandled] == [_ITEMS[1]]