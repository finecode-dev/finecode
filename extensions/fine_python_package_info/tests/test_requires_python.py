"""Tests for the shared reader of ``project.requires-python``."""

from __future__ import annotations

import pytest
from finecode_extension_api import code_action

from fine_python_package_info.requires_python import support_range


def test_open_upper_bound_reports_an_open_range() -> None:
    # the correct form for a published package: no upper bound at all. Open is the
    # answer, not a missing one -- there is no ceiling to report because none was
    # promised.
    assert support_range(">=3.11") == ("3.11", None)


def test_bounded_range_reports_both_ends() -> None:
    assert support_range(">=3.11,<3.14") == ("3.11", "3.13")


def test_exclusive_lower_bound_keeps_the_series_it_excludes_the_first_patch_of() -> (
    None
):
    # >3.10 excludes 3.10.0 but admits 3.10.1, and a series stands for its newest patch,
    # so 3.10 stays the floor. The interpreter axis reads it the same way, which is the
    # property that matters: one rule, so the two derivations cannot disagree.
    assert support_range(">3.10") == ("3.10", None)


def test_compatible_release_of_a_minor_is_open_within_its_major() -> None:
    # ~=3.11 means >=3.11, <4 -- every later 3.x is admitted, so the 3.x line has no
    # ceiling even though the specifier does have an upper bound
    assert support_range("~=3.11") == ("3.11", None)


def test_compatible_release_of_a_patch_is_pinned_to_one_minor() -> None:
    # ~=3.11.0 means >=3.11.0, <3.12
    assert support_range("~=3.11.0") == ("3.11", "3.11")


def test_wildcard_equality_is_pinned_to_one_minor() -> None:
    assert support_range("==3.12.*") == ("3.12", "3.12")


def test_patch_level_floor_keeps_its_own_minor() -> None:
    # a minor series stands for its newest patch, so >=3.11.4 still supports the 3.11
    # series. This is the rule the interpreter axis matches with -- the two derivations
    # must agree here or a project's lint target and its oldest tested interpreter drift
    # apart by one minor.
    assert support_range(">=3.11.4") == ("3.11", None)


def test_patch_level_ceiling_drops_the_minor_it_cannot_satisfy() -> None:
    # the other side of the same rule: the newest 3.11 patch violates <3.11.5, so 3.11
    # is not a series this project can be built on
    assert support_range(">=3.10,<3.11.5") == ("3.10", "3.10")


def test_hole_in_the_middle_does_not_move_the_ends() -> None:
    # only the ends are represented; a project excluding one minor keeps its range
    assert support_range(">=3.10,!=3.11.*,<3.13") == ("3.10", "3.12")


def test_ceiling_without_a_floor_reports_no_floor() -> None:
    # nothing is ruled out below, so there is no declared floor to report. Answering
    # with the bottom of the scan instead would invent a promise the project never made
    # and hand every tool a language level decades below what it runs on.
    assert support_range("<3.14") == (None, "3.13")


def test_open_ended_ceiling_without_a_floor_reports_neither_end() -> None:
    assert support_range("<4") == (None, None)


def test_range_spanning_majors_reports_the_ceiling_it_has() -> None:
    # the ends are read off the whole scan, not off the floor's major line: asking only
    # within the 2.x line answers "open" here, which is the opposite of what <3.14 says
    assert support_range(">=2.7,<3.14") == ("2.7", "3.13")


def test_invalid_specifier_names_the_offending_value() -> None:
    with pytest.raises(code_action.ActionFailedException, match="requires-python"):
        support_range("three point eleven")


def test_specifier_admitting_no_series_is_an_error() -> None:
    # an exact patch pin cannot be satisfied by any provisionable minor series, which is
    # the same answer the interpreter axis gives it
    with pytest.raises(
        code_action.ActionFailedException, match="no Python minor series"
    ):
        support_range("==3.11.2")
