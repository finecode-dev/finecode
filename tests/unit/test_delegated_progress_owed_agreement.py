"""The WM owed rule and the ER report mapping agree on every P4 row.

The wire value carries no owed flag, so the WM classifies it itself while the
ER maps it to a message. Both implement the same P4 table; this test pins
them together: ``is_owed_progress_value`` is true exactly when
``delegated_progress_message`` yields a report.
"""

from __future__ import annotations

import typing

from finecode_extension_runner.impls import _delegated_progress as er_mapping
from finecode.wm_server import domain
from finecode.wm_server.services.run_service import delegated_progress as wm_owed


def _rows() -> list[dict[str, object]]:
    return [
        {"type": "begin", "message": "m", "title": "t"},
        {"type": "begin", "message": "", "title": "t"},
        {"type": "begin", "message": None, "title": "t"},
        {"type": "begin", "message": 3, "title": "t"},
        {"type": "begin", "title": "t"},
        {"type": "begin", "message": None, "title": None},
        {"type": "begin", "message": 3, "title": 4},
        {"type": "begin"},
        {"type": "report", "message": "m"},
        {"type": "report", "message": ""},
        {"type": "report"},
        {"type": "report", "message": None},
        {"type": "report", "message": 3},
        {"type": "end", "message": "m"},
        {"type": "end", "message": ""},
        {"type": "end"},
        {"type": "end", "message": None},
        {"type": "end", "message": 3},
        {"type": "bogus", "message": "m"},
        {"message": "m"},
        {},
    ]


def test_owed_agrees_with_mapping_on_every_row() -> None:
    """Owed is exactly mapped-to-a-report, on every P4 row."""
    for row in _rows():
        message = er_mapping.delegated_progress_message(dict(row))
        owed = wm_owed.is_owed_progress_value(
            typing.cast(domain.ProgressRawValue, row)
        )
        assert owed == (message is not None), row


def test_non_dict_values_owe_nothing() -> None:
    """Values that are not dicts cannot name a report, on either side."""
    for raw in (None, "report", 42, ["type", "report"]):
        assert wm_owed.is_owed_progress_value(
            typing.cast(domain.ProgressRawValue, raw)
        ) is False
