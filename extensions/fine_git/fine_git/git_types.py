"""Shared types for git status and diff actions.

`GitChangeKind` is the single vocabulary both `get_git_status` and
`get_git_diff` use to describe a file's change state, so a caller correlating
a status entry with a diff entry sees the same kind spelled the same way in
both places rather than two different enumerations.
"""

import enum


class GitChangeKind(enum.StrEnum):
    UNMODIFIED = "unmodified"
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNTRACKED = "untracked"
    IGNORED = "ignored"
    UNMERGED = "unmerged"


_STATUS_CHAR_TO_CHANGE_KIND: dict[str, GitChangeKind] = {
    " ": GitChangeKind.UNMODIFIED,
    "M": GitChangeKind.MODIFIED,
    "A": GitChangeKind.ADDED,
    "D": GitChangeKind.DELETED,
    "R": GitChangeKind.RENAMED,
    "C": GitChangeKind.COPIED,
    "T": GitChangeKind.TYPE_CHANGED,
    "?": GitChangeKind.UNTRACKED,
    "!": GitChangeKind.IGNORED,
    "U": GitChangeKind.UNMERGED,
}


def change_kind_from_status_char(char: str) -> GitChangeKind:
    """Map a single porcelain v1 status character (the X or Y column) to a `GitChangeKind`.

    Raises:
        ValueError: `char` is not a recognized porcelain status character.
    """
    try:
        return _STATUS_CHAR_TO_CHANGE_KIND[char]
    except KeyError:
        raise ValueError(f"Unknown git status character: {char!r}") from None
