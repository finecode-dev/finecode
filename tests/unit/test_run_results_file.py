import json
import pathlib

import pytest

from finecode.cli_app import utils
from finecode.cli_app.cli import RUN_RESULTS_FILE_VERSION, _write_run_results_file
from finecode.wm_server.runner import runner_client


def _response(
    payload: dict | None, return_code: int = 0
) -> runner_client.RunActionResponse:
    return runner_client.RunActionResponse(
        result_by_format={} if payload is None else {"json": payload},
        return_code=return_code,
    )


def _result(
    result_by_project: dict[pathlib.Path, dict[str, runner_client.RunActionResponse]],
    scope_by_action_source: dict[str, str | None] | None = None,
    return_code: int = 0,
    project_paths_requested: list[str] | None = None,
) -> utils.RunActionsResult:
    return utils.RunActionsResult(
        output="",
        return_code=return_code,
        result_by_project=result_by_project,
        scope_by_action_source=scope_by_action_source,
        project_paths_requested=project_paths_requested,
    )


def test_records_the_declared_scope_of_each_action(tmp_path: pathlib.Path) -> None:
    """Without this, the host key is indistinguishable from a project key.

    A workspace-scoped action files its result under the project that hosted it,
    so `{"/ws": ...}` here means "everything this run linted", not "the /ws
    project's diagnostics".
    """
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {
                pathlib.Path("/ws"): {
                    "fine_lint.LintAction": _response({"messages": {}})
                }
            },
            scope_by_action_source={"fine_lint.LintAction": "workspace"},
        ),
        payload={"project_paths": ["file:///ws/proj"]},
        projects_requested=None,
        return_code=0,
    )

    document = json.loads(target.read_text())
    assert document["finecode_results_version"] == RUN_RESULTS_FILE_VERSION
    action = document["actions"]["fine_lint.LintAction"]
    assert action["scope"] == "workspace"
    assert list(action["results"]) == ["/ws"]


def test_records_the_request_as_well_as_the_outcome(tmp_path: pathlib.Path) -> None:
    """`projects_requested` separates "found nothing" from "dispatched nowhere"."""
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {
                pathlib.Path("/ws/proj"): {
                    "fine_git.GetGitStatusAction": _response({"changes": []})
                }
            },
            scope_by_action_source={"fine_git.GetGitStatusAction": "project"},
            project_paths_requested=["/ws/proj"],
        ),
        payload={"include_ignored": True},
        projects_requested=["proj"],
        return_code=0,
    )

    document = json.loads(target.read_text())
    assert document["projects_requested"] == ["proj"]
    assert document["payload"] == {"include_ignored": True}
    assert document["actions"]["fine_git.GetGitStatusAction"]["scope"] == "project"


def test_requested_paths_are_joinable_with_the_result_keys(
    tmp_path: pathlib.Path,
) -> None:
    """`--project` takes names; every key under `results` is a path.

    Recording only the names leaves "which requested project produced nothing"
    unanswerable, which is the attribution the file exists to provide.
    """
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {pathlib.Path("/ws/a"): {"x.Action": _response({"n": 1})}},
            scope_by_action_source={"x.Action": "project"},
            project_paths_requested=["/ws/a", "/ws/b"],
        ),
        payload={},
        projects_requested=["a", "b"],
        return_code=0,
    )

    document = json.loads(target.read_text())
    produced = set(document["actions"]["x.Action"]["results"])
    assert set(document["project_paths_requested"]) - produced == {"/ws/b"}


def test_a_second_run_replaces_the_file_rather_than_merging(
    tmp_path: pathlib.Path,
) -> None:
    """The point of the per-run file, against `cache/.../<action>.json`.

    The shared cache is read-modify-written, so it accumulates entries for
    projects the run in hand never touched and a reader cannot tell those from
    the current ones. This file describes one run and nothing else.
    """
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {pathlib.Path("/ws/old"): {"a.Action": _response({"n": 1})}},
            scope_by_action_source={"a.Action": "project"},
        ),
        payload={},
        projects_requested=["old"],
        return_code=0,
    )
    _write_run_results_file(
        target,
        _result(
            {pathlib.Path("/ws/new"): {"a.Action": _response({"n": 2})}},
            scope_by_action_source={"a.Action": "project"},
        ),
        payload={},
        projects_requested=["new"],
        return_code=0,
    )

    document = json.loads(target.read_text())
    assert list(document["actions"]["a.Action"]["results"]) == ["/ws/new"]


def test_a_failed_run_replaces_an_earlier_run_s_file(tmp_path: pathlib.Path) -> None:
    """A run that produced nothing must still overwrite what is there.

    The earlier document is complete, well-formed and carries the same version,
    so a reader that finds it left behind reports the wrong run's outcome as
    this one's.
    """
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {pathlib.Path("/ws"): {"a.Action": _response({"n": 1})}},
            scope_by_action_source={"a.Action": "project"},
        ),
        payload={},
        projects_requested=None,
        return_code=0,
    )
    _write_run_results_file(
        target,
        None,
        payload={},
        projects_requested=["proj"],
        return_code=1,
    )

    document = json.loads(target.read_text())
    assert document["actions"] == {}
    assert document["return_code"] == 1
    assert document["projects_requested"] == ["proj"]
    assert document["project_paths_requested"] is None


def test_groups_multi_project_results_under_one_action(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {
                pathlib.Path("/ws/a"): {"x.Action": _response({"n": 1})},
                pathlib.Path("/ws/b"): {"x.Action": _response({"n": 2})},
            },
            scope_by_action_source={"x.Action": "project"},
        ),
        payload={},
        projects_requested=None,
        return_code=0,
    )

    results = json.loads(target.read_text())["actions"]["x.Action"]["results"]
    assert sorted(results) == ["/ws/a", "/ws/b"]


def test_records_a_return_code_per_action_and_project(
    tmp_path: pathlib.Path,
) -> None:
    """The single top-level code cannot say which of them failed."""
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {
                pathlib.Path("/ws/a"): {"x.Action": _response({"n": 1}, return_code=0)},
                pathlib.Path("/ws/b"): {"x.Action": _response({"n": 2}, return_code=1)},
            },
            scope_by_action_source={"x.Action": "project"},
            return_code=1,
        ),
        payload={},
        projects_requested=None,
        return_code=1,
    )

    results = json.loads(target.read_text())["actions"]["x.Action"]["results"]
    assert results["/ws/a"]["return_code"] == 0
    assert results["/ws/b"]["return_code"] == 1
    assert results["/ws/b"]["result"] == {"n": 2}


def test_an_action_without_a_json_result_is_recorded_as_null(
    tmp_path: pathlib.Path,
) -> None:
    """A fully streamed matrixed action merges to no json payload at all.

    Aborting there would fail a run that already succeeded, after its output was
    printed.
    """
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result(
            {pathlib.Path("/ws"): {"x.Action": _response(None)}},
            scope_by_action_source={"x.Action": "project"},
        ),
        payload={},
        projects_requested=None,
        return_code=0,
    )

    results = json.loads(target.read_text())["actions"]["x.Action"]["results"]
    assert results["/ws"]["result"] is None


def test_unresolved_scope_is_recorded_as_null(tmp_path: pathlib.Path) -> None:
    """`None` must reach the reader as "unknown", never as a default scope."""
    target = tmp_path / "run.json"

    _write_run_results_file(
        target,
        _result({pathlib.Path("/ws"): {"x.Action": _response({})}}),
        payload={},
        projects_requested=None,
        return_code=0,
    )

    assert json.loads(target.read_text())["actions"]["x.Action"]["scope"] is None


def test_creates_missing_parent_directories(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "nested" / "dir" / "run.json"

    _write_run_results_file(
        target,
        _result({pathlib.Path("/ws"): {"x.Action": _response({})}}),
        payload={},
        projects_requested=None,
        return_code=0,
    )

    assert target.exists()


def test_a_failed_write_leaves_no_temporary_file_behind(
    tmp_path: pathlib.Path,
) -> None:
    """The rename-into-place write must not litter on the way out."""
    target = tmp_path / "dir-in-the-way"
    target.mkdir()

    with pytest.raises(OSError):
        _write_run_results_file(
            target,
            _result({pathlib.Path("/ws"): {"x.Action": _response({})}}),
            payload={},
            projects_requested=None,
            return_code=0,
        )

    assert list(tmp_path.iterdir()) == [target]
