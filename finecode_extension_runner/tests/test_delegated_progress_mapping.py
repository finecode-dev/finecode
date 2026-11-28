"""Every P4 row maps to its parent report message through one shared function."""

from __future__ import annotations

import pytest

from finecode_extension_runner.impls import _delegated_progress


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "begin", "message": "m", "title": "t"}, "m"),
        ({"type": "begin", "message": "", "title": "t"}, ""),
        ({"type": "begin", "message": None, "title": "t"}, "t"),
        ({"type": "begin", "message": 3, "title": "t"}, "t"),
        ({"type": "begin", "title": "t"}, "t"),
        ({"type": "begin", "message": None, "title": None}, None),
        ({"type": "begin", "message": 3, "title": 4}, None),
        ({"type": "begin"}, None),
        ({"type": "report", "message": "m"}, "m"),
        ({"type": "report", "message": ""}, ""),
        ({"type": "report"}, None),
        ({"type": "report", "message": None}, None),
        ({"type": "report", "message": 3}, None),
        ({"type": "end", "message": "m"}, "m"),
        ({"type": "end", "message": ""}, ""),
        ({"type": "end"}, "Delegated progress ended"),
        ({"type": "end", "message": None}, "Delegated progress ended"),
        ({"type": "end", "message": 3}, "Delegated progress ended"),
        ({"type": "bogus", "message": "m"}, None),
        ({"message": "m"}, None),
        ({}, None),
        # Percentage, total and cancellable never affect the outcome.
        (
            {
                "type": "begin",
                "message": "m",
                "title": "t",
                "percentage": 50,
                "total": 10,
                "cancellable": True,
            },
            "m",
        ),
        ({"type": "report", "message": "m", "percentage": 99}, "m"),
        ({"type": "end", "percentage": 1}, "Delegated progress ended"),
    ],
)
def test_p4_row(event: dict[str, object], expected: str | None) -> None:
    """The shared table answers every P4 row, including the neutral end text."""
    assert _delegated_progress.delegated_progress_message(event) == expected


def test_end_text_constant() -> None:
    """The neutral end text is exact, with no punctuation."""
    assert _delegated_progress.DELEGATED_PROGRESS_END_TEXT == "Delegated progress ended"


def test_pending_limit() -> None:
    """The per-call pending-report bound is fixed at 100."""
    assert _delegated_progress.DELEGATED_PROGRESS_PENDING_LIMIT == 100
