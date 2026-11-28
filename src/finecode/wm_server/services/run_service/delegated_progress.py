"""Delegated-progress correlation for the WM side of a delegated call.

The per-call pending bound and the rule deciding which decoded
``$/progress`` values owe the parent a report. The ER applies the same table through
its own mapping
(``impls/_delegated_progress.py``); ``test_delegated_progress_owed_agreement``
pins the two together row by row.
"""

from __future__ import annotations

import typing

from finecode.wm_server import domain

DELEGATED_PROGRESS_PENDING_LIMIT: typing.Final = 100


def is_owed_progress_value(value: domain.ProgressRawValue) -> bool:
    """Whether a decoded progress value owes the parent a report.

    ``begin`` is owed when its ``message`` is a string, else when its
    ``title`` is one; ``report`` is owed only when its ``message`` is a
    string; ``end`` is always owed (a message-less end still delivers the
    neutral end text). Anything else — a non-dict value, a missing or
    unknown ``type`` — owes nothing.
    """
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    message = value.get("message")
    if kind == "begin":
        return isinstance(message, str) or isinstance(value.get("title"), str)
    if kind == "report":
        return isinstance(message, str)
    if kind == "end":
        return True
    return False
