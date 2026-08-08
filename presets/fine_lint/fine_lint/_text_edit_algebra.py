"""Pure edit algebra for applying batches of ``TextEdit`` objects to file content.

No I/O and no knowledge of providers, selections, or files on disk -- everything
here operates on plain ``Range``/``TextEdit`` values and ``str`` content, so it is
unit-testable directly. ``apply_code_actions_handler`` is the only caller.

Design note ``design-notes/applying-code-actions.md`` D2-D4 is the reasoning this
module embodies:

- Ranges are LSP-style and half-open: ``[start, end)``.
- Two edits *overlap* when their ranges intersect. Two zero-width edits
  (``start == end``) at the *same* position also count as overlapping (D4) --
  they do not intersect by range arithmetic, but the resulting text order
  depends on which one is applied first, so they must not both be accepted.
- Applying a batch of non-overlapping edits back-to-front (descending by start
  position) against one base version of the content is what makes multiple
  providers' edits correct without re-analysis (D2): every edit's range
  addresses the *original* content, and since nothing before an unprocessed
  edit's start has been touched yet, its range stays valid all the way through.
"""

from __future__ import annotations

import re

from fine_lint.lint_fix import Position, Range, TextEdit

_PositionKey = tuple[int, int]

# LSP recognises exactly three line terminators. `str.splitlines` additionally
# breaks on several other control and Unicode separators (vertical tab,
# form feed, the C1 NEL, and the Unicode line/paragraph separators), so a
# file containing any of them (a form feed used as a section separator is
# the common one in Python) would be numbered differently here than by the
# server that produced the edit, and an edit would validate against -- and
# be applied to -- the wrong line.
_LINE_TERMINATOR_RE = re.compile(r"\r\n|\r|\n")
_LINE_TERMINATOR_CAPTURE_RE = re.compile(r"(\r\n|\r|\n)")


def _position_key(position: Position) -> _PositionKey:
    return (position.line, position.character)


def ranges_overlap(a: Range, b: Range) -> bool:
    """True iff *a* and *b* have no simultaneous interpretation (D2-D4).

    Half-open range intersection, plus the D4 special case: two zero-width
    edits at exactly the same position are treated as overlapping even though
    their ranges do not intersect, because the text order between them depends
    on application order.
    """
    a_start, a_end = _position_key(a.start), _position_key(a.end)
    b_start, b_end = _position_key(b.start), _position_key(b.end)

    if a_start == a_end and b_start == b_end and a_start == b_start:
        return True

    return max(a_start, b_start) < min(a_end, b_end)


def edits_conflict(candidates: list[TextEdit], accepted: list[TextEdit]) -> bool:
    """True iff any edit in *candidates* overlaps any edit already in *accepted*."""
    return any(
        ranges_overlap(candidate.range, other.range)
        for candidate in candidates
        for other in accepted
    )


def _lines(content: str) -> list[str]:
    """Split *content* into LSP-style lines: one entry per line, with a trailing
    empty line when the content ends with a line terminator (the position right
    after the final newline is itself a valid, empty line).

    Splits on the three LSP line terminators only -- see ``_LINE_TERMINATOR_RE``
    for why ``str.splitlines`` is the wrong tool here."""
    return _LINE_TERMINATOR_RE.split(content)


def _lines_keepends(content: str) -> list[str]:
    """Same split as ``_lines``, but each line keeps its own terminator, so that
    ``"".join(_lines_keepends(content)) == content``."""
    parts = _LINE_TERMINATOR_CAPTURE_RE.split(content)
    # `parts` alternates line, terminator, line, ..., always ending on a line.
    return [
        parts[index] + (parts[index + 1] if index + 1 < len(parts) else "")
        for index in range(0, len(parts), 2)
    ]


def _position_within_content(position: Position, lines: list[str]) -> bool:
    if position.line < 0 or position.line >= len(lines):
        return False
    # character == len(line) is valid: end of line.
    return 0 <= position.character <= len(lines[position.line])


def is_valid_edit_range(edit_range: Range, content: str) -> bool:
    """True iff *edit_range* is well-formed and addresses a real position in
    *content*: ``start <= end``, both positions' lines exist, and both
    positions' characters are within (or exactly at the end of) their line."""
    if _position_key(edit_range.start) > _position_key(edit_range.end):
        return False
    lines = _lines(content)
    return _position_within_content(
        edit_range.start, lines
    ) and _position_within_content(edit_range.end, lines)


def _apply_single_edit(content: str, edit: TextEdit) -> str:
    lines = _lines_keepends(content)
    start_line = edit.range.start.line
    end_line = edit.range.end.line
    start_char = edit.range.start.character
    end_char = edit.range.end.character
    while len(lines) <= end_line:
        lines.append("")
    prefix = lines[start_line][:start_char]
    suffix = lines[end_line][end_char:]
    return (
        "".join(lines[:start_line])
        + prefix
        + edit.new_text
        + suffix
        + "".join(lines[end_line + 1 :])
    )


def apply_edits(content: str, edits: list[TextEdit]) -> str:
    """Apply *edits* to *content*, all interpreted against *content* as it was
    before any of them were applied (D2).

    Edits are sorted descending by start position and applied back-to-front:
    nothing before an as-yet-unapplied edit's start position has been touched
    by an already-applied edit, so every edit's range stays valid against the
    *current* content at the moment it is applied. Do not apply front-to-back
    and do not try to compensate offsets -- that is a different (also correct)
    algorithm, but this module intentionally implements only this one.

    Ties on start position are broken by end position, longest first, so that a
    zero-width insertion at ``p`` is applied *after* a replacement starting at
    ``p`` -- the two do not overlap by ``ranges_overlap`` and so can legitimately
    share a batch, but ordering them the other way round applies the replacement
    against content the insertion has already shifted, and the replacement's
    range then swallows the inserted text. Sorting on start alone left the tie
    order equal to input order, which is not a property of the edits at all.

    *edits* must be mutually non-overlapping (see ``ranges_overlap`` /
    ``edits_conflict``); this function does not check that itself.
    """
    ordered = sorted(
        edits,
        key=lambda edit: (
            _position_key(edit.range.start),
            _position_key(edit.range.end),
        ),
        reverse=True,
    )
    for edit in ordered:
        content = _apply_single_edit(content, edit)
    return content
