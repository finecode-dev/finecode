from finecode.cli_app.log_render import render_log_records


def test_render_log_records_two_records_in_order() -> None:
    params = {
        "records": [
            {"level": "INFO", "source": "wm", "group": "finecode.wm_server", "message": "first"},
            {"level": "ERROR", "source": "wm", "group": "finecode.wm_server", "message": "second"},
        ]
    }
    assert render_log_records(params) == [
        "[INFO] wm finecode.wm_server: first",
        "[ERROR] wm finecode.wm_server: second",
    ]


def test_render_log_records_dropped_count_appends_trailing_line() -> None:
    params = {
        "records": [
            {"level": "INFO", "source": "wm", "group": "g", "message": "m"},
        ],
        "droppedCount": 4,
    }
    lines = render_log_records(params)
    assert lines[-1] == "... dropped 4 log records"
    assert len(lines) == 2


def test_render_log_records_empty_records_no_dropped_returns_empty_list() -> None:
    assert render_log_records({"records": []}) == []
    assert render_log_records({}) == []
