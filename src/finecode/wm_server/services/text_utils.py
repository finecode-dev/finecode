"""Utilities for applying LSP text document content changes to a string."""

from __future__ import annotations

from finecode.wm_server.runner import runner_client


def apply_text_changes(
    text: str,
    changes: list[
        runner_client.TextDocumentContentChangePartial
        | runner_client.TextDocumentContentChangeWholeDocument
    ],
) -> str:
    """Apply a sequence of LSP content changes to *text* and return the result.

    LSP character offsets are UTF-16 code unit counts.  For files that contain
    only BMP characters (U+0000–U+FFFF) each character maps to exactly one
    UTF-16 code unit, so plain string indexing is correct.  Files with
    characters outside the BMP (e.g. emoji) may see off-by-one errors in the
    rare case where a range boundary falls inside or immediately after such a
    character; this is an accepted limitation for now.
    """
    for change in changes:
        if isinstance(change, runner_client.TextDocumentContentChangeWholeDocument):
            text = change.text
        else:
            text = _apply_partial_change(text, change)
    return text


def _apply_partial_change(
    text: str, change: runner_client.TextDocumentContentChangePartial
) -> str:
    lines = text.split("\n")

    start_line = change.range.start.line
    start_char = change.range.start.character
    end_line = change.range.end.line
    end_char = change.range.end.character

    # Build the prefix: everything before the start position.
    prefix = "\n".join(lines[:start_line])
    if start_line > 0:
        prefix += "\n"
    if start_line < len(lines):
        prefix += lines[start_line][:start_char]

    # Build the suffix: everything after the end position.
    suffix = ""
    if end_line < len(lines):
        suffix = lines[end_line][end_char:]
        if end_line + 1 < len(lines):
            suffix += "\n" + "\n".join(lines[end_line + 1 :])

    return prefix + change.text + suffix
