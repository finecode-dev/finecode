from __future__ import annotations

import pathlib

from finecode.cli_app.commands.run_cmd import _build_streaming_result, _format_project_block


def _action_result(text: str, return_code: int = 0) -> dict:
    return {"resultByFormat": {"string": text}, "returnCode": return_code}


def test_empty_content_returns_none_regardless_of_header() -> None:
    """A partial whose action produced no renderable text (e.g. nothing to
    report for that project) must not print a bare header with an empty body."""
    empty = {"src.A": _action_result("")}

    assert _format_project_block("/root", empty, {"src.A": "A"}, True) is None
    assert _format_project_block("/root", empty, {"src.A": "A"}, False) is None


def test_header_shown_by_default() -> None:
    results = {"src.A": _action_result("boom\n", return_code=1)}

    block = _format_project_block("/root/project", results, {"src.A": "A"})

    assert block is not None
    assert "/root/project" in block
    assert "boom" in block


def test_header_suppressed_for_workspace_scoped_runs() -> None:
    """Workspace-scoped actions stream every partial tagged with the same root
    project path; repeating that header per partial is pure noise, so callers
    can opt out of it while keeping the actual content."""
    results = {"src.A": _action_result("boom\n", return_code=1)}

    block = _format_project_block(
        "/root/project", results, {"src.A": "A"}, show_project_header=False
    )

    assert block is not None
    assert "/root/project" not in block
    assert block.strip() == "boom"


def test_multiple_actions_prefix_each_with_its_display_name() -> None:
    results = {
        "src.A": _action_result("a-out\n"),
        "src.B": _action_result("b-out\n"),
    }

    block = _format_project_block(
        "/root", results, {"src.A": "lint", "src.B": "format"}
    )

    assert block is not None
    assert "lint" in block
    assert "format" in block
    assert "a-out" in block
    assert "b-out" in block


def test_build_streaming_result_extracts_result_by_format_and_return_code() -> None:
    """``_build_streaming_result`` is fed the WM's merged ``results`` payload
    (project -> action source -> {resultByFormat, returnCode}); it must expose
    each action's json/string data and return code, keyed by ``pathlib.Path``."""
    merged = {
        "/proj": {
            "src.A": {
                "resultByFormat": {"json": {"messages": {"f": []}}},
                "returnCode": 2,
            }
        }
    }

    result = _build_streaming_result(merged, overall_return_code=2)

    assert result.output == ""
    assert result.return_code == 2
    project_path = pathlib.Path("/proj")
    assert project_path in result.result_by_project
    response = result.result_by_project[project_path]["src.A"]
    assert response.json() == {"messages": {"f": []}}
    assert response.return_code == 2


def test_build_streaming_result_handles_no_results() -> None:
    result = _build_streaming_result({}, overall_return_code=0)

    assert result.result_by_project == {}
    assert result.return_code == 0
