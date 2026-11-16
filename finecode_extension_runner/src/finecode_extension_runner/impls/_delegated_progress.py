"""ER-private mapping from a child progress event to a parent report message.

Both the local forwarder and the WM-path receipt use this one table, so the two paths
can never disagree about which child events owe the
parent a ``report``. The input is the decoded ``$/progress`` value shape: a
dict with a ``type`` of ``begin``/``report``/``end``. The local forwarder
builds that same dict shape from its typed sender arguments before calling
here.
"""

from __future__ import annotations

import typing

DELEGATED_PROGRESS_END_TEXT: typing.Final = "Delegated progress ended"
DELEGATED_PROGRESS_PENDING_LIMIT: typing.Final = 100
RELEASE_WAIT_SECONDS: typing.Final = 2.0


def delegated_progress_message(event: dict[str, object]) -> str | None:
    """Return the parent report message owed to *event*, or ``None``.

    ``None`` means the event owes no report: it is dropped (malformed and
    unknown events are additionally logged by the caller). ``percentage``,
    ``total`` and ``cancellable`` are never read. An empty string is a string
    and is reported as ``\"\"``.
    """
    event_type = event.get("type")
    message = event.get("message")
    if event_type == "begin":
        if isinstance(message, str):
            return message
        title = event.get("title")
        if isinstance(title, str):
            return title
        return None
    if event_type == "report":
        if isinstance(message, str):
            return message
        return None
    if event_type == "end":
        if isinstance(message, str):
            return message
        return DELEGATED_PROGRESS_END_TEXT
    return None
