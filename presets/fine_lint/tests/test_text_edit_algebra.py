"""Tests for the pure edit algebra ``apply_code_actions`` applies a batch with."""

from __future__ import annotations

from fine_lint._text_edit_algebra import (
    apply_edits,
    edits_conflict,
    is_valid_edit_range,
    ranges_overlap,
)
from fine_lint.lint_fix import Position, Range, TextEdit


def _range(start_line: int, start_char: int, end_line: int, end_char: int) -> Range:
    return Range(
        start=Position(line=start_line, character=start_char),
        end=Position(line=end_line, character=end_char),
    )


def _edit(
    start_line: int, start_char: int, end_line: int, end_char: int, new_text: str
) -> TextEdit:
    return TextEdit(
        range=_range(start_line, start_char, end_line, end_char), new_text=new_text
    )


def test_applying_back_to_front_produces_correct_content_where_front_to_back_would_not() -> (
    None
):
    """Two providers' edits on the same line must combine correctly without either
    provider re-deriving its position against the other's output.

    If a batch were applied in the order the edits appear (left to right) using
    each edit's originally-computed range, the second edit would land on
    whatever character now sits at its old offset -- not the character it was
    actually meant to replace -- because the first edit already shifted
    everything after it. A user applying two independent quickfixes in one
    request would see the wrong character silently rewritten.
    """
    content = "abcdef"
    replace_a_with_xyz = _edit(0, 0, 0, 1, "XYZ")  # "a" -> "XYZ" (longer)
    replace_d_with_q = _edit(0, 3, 0, 4, "Q")  # "d" -> "Q"

    result = apply_edits(content, [replace_a_with_xyz, replace_d_with_q])

    assert result == "XYZbcQef"
    # What naively re-using the second edit's original offset against the
    # already-shifted string would produce instead -- it hits "b", not "d".
    wrongly_shifted = "XYZbcdef"[:3] + "Q" + "XYZbcdef"[4:]
    assert wrongly_shifted == "XYZQcdef"
    assert result != wrongly_shifted


def test_non_overlapping_ranges_do_not_conflict() -> None:
    """Two edits addressing disjoint parts of a file must be free to combine, or a
    user with two unrelated quickfixes selected would be blocked for no reason."""
    assert ranges_overlap(_range(0, 0, 0, 2), _range(0, 5, 0, 7)) is False


def test_intersecting_ranges_conflict() -> None:
    """Two edits that both touch the same text have no simultaneous
    interpretation; accepting both would silently pick one provider's answer
    over the other depending on write order."""
    assert ranges_overlap(_range(0, 0, 0, 5), _range(0, 3, 0, 7)) is True


def test_adjacent_touching_ranges_do_not_conflict() -> None:
    """Ranges are half-open, so an edit ending exactly where another begins must
    not be treated as a conflict -- refusing it would make two independent,
    non-destructive edits (e.g. two separate insertions next to each other)
    impossible to apply together."""
    assert ranges_overlap(_range(0, 0, 0, 3), _range(0, 3, 0, 5)) is False


def test_same_position_zero_width_edits_conflict() -> None:
    """Two insertions at the exact same point have no simultaneous
    interpretation: which one ends up first in the text depends on application
    order, so accepting both would make the result depend on an arbitrary
    choice rather than on what either provider intended."""
    assert ranges_overlap(_range(0, 2, 0, 2), _range(0, 2, 0, 2)) is True


def test_zero_width_edits_at_different_positions_do_not_conflict() -> None:
    """Two independent insertions at different points in a file are fully
    compatible and must not be refused."""
    assert ranges_overlap(_range(0, 2, 0, 2), _range(0, 5, 0, 5)) is False


def test_edits_conflict_checks_every_pair() -> None:
    """A candidate selection with several edits must be refused if *any* of them
    collides with an already-accepted edit, not just its first one -- otherwise
    a later edit in the same selection could silently corrupt an already-applied
    fix."""
    accepted = [_edit(0, 0, 0, 3, "x")]
    candidates = [_edit(1, 0, 1, 1, "y"), _edit(0, 1, 0, 2, "z")]

    assert edits_conflict(candidates, accepted) is True


def test_range_with_start_after_end_is_invalid() -> None:
    """A malformed range must be rejected before it reaches disk, or applying it
    could corrupt the file in a way with no clear meaning."""
    assert is_valid_edit_range(_range(0, 5, 0, 2), "abcdef") is False


def test_range_on_a_line_that_does_not_exist_is_invalid() -> None:
    """An edit addressing a line past the end of the file names a position that
    was never in the content it was computed against, and must not be applied
    as if it were."""
    assert is_valid_edit_range(_range(5, 0, 5, 0), "one line only\n") is False


def test_character_past_the_end_of_its_line_is_invalid() -> None:
    """An edit whose column is beyond the line's actual length names a position
    that does not exist in the file's current content."""
    assert is_valid_edit_range(_range(0, 0, 0, 999), "short\n") is False


def test_character_exactly_at_line_end_is_valid() -> None:
    """A position at the end of a line (one past its last character) is a real,
    addressable position -- e.g. for appending text -- and must not be rejected."""
    assert is_valid_edit_range(_range(0, 5, 0, 5), "short\n") is True


def test_an_insertion_and_a_replacement_at_one_position_ignore_input_order() -> None:
    """A zero-width insertion at ``p`` and a replacement starting at ``p`` do not
    overlap, so ``edits_conflict`` lets them share a batch. Applying them must
    then preserve both, whichever order the caller happened to list them in --
    sorting on start position alone left the tie broken by input order, and the
    order that applied the insertion first let the replacement's range swallow
    the inserted text."""
    insertion = _edit(0, 1, 0, 1, "X")
    replacement = _edit(0, 1, 0, 2, "Y")

    assert edits_conflict([insertion], [replacement]) is False
    assert apply_edits("abc", [insertion, replacement]) == "aXYc"
    assert apply_edits("abc", [replacement, insertion]) == "aXYc"


def test_a_form_feed_is_not_a_line_break() -> None:
    """LSP splits lines on \\n, \\r\\n and \\r only. ``str.splitlines`` also breaks
    on a form feed -- a common Emacs-style section separator in Python -- which
    would number every line below it differently here than in the server that
    produced the edit, so an edit would validate against, and be applied to, the
    wrong line."""
    content = "first\n\x0csecond\nthird\n"

    # Line 2 is "third" under LSP numbering; splitlines() would call it line 3.
    assert (
        apply_edits(content, [_edit(2, 0, 2, 5, "THIRD")])
        == "first\n\x0csecond\nTHIRD\n"
    )
    # And the line past the real end is still correctly rejected.
    assert is_valid_edit_range(_range(4, 0, 4, 0), content) is False


def test_a_carriage_return_only_file_is_split_on_its_line_endings() -> None:
    """Old-style ``\\r`` line endings are LSP line terminators and must be counted
    as such, not left inside a single oversized line."""
    assert apply_edits("a\rb\rc", [_edit(1, 0, 1, 1, "B")]) == "a\rB\rc"
