"""When pytest exits with a code that is not a normal test outcome, the
returned error must carry both output streams.

Regression for issue 967: a collection error exits 2. The traceback is on
stdout while stderr holds only an unrelated plugin warning; reporting
`stderr or stdout` dropped the traceback.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fine_test.run_tests_action import (
    RunTestsRunContext,
    RunTestsRunPayload,
    TestOutcome,
)
from finecode_extension_api import code_action
from finecode_extension_runner.testing import NoOpLogger

from fine_python_pytest.run_tests_handler import (
    PytestRunTestsHandler,
    PytestRunTestsHandlerConfig,
)

_STDOUT = "ImportError: cannot import name '_derive_service_name'"
_STDERR = "PytestDeprecationWarning: asyncio_default_fixture_loop_scope is unset"


class _Process:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self._exit_code, self._stdout, self._stderr = exit_code, stdout, stderr

    def get_exit_code(self) -> int:
        return self._exit_code

    def get_output(self) -> str:
        return self._stdout

    def get_error_output(self) -> str:
        return self._stderr

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _CommandRunner:
    """Writes `report` (if any) to the path pytest was told to use for its
    JSON report, as the real pytest-json-report plugin would."""

    def __init__(self, process: _Process, report: dict | None = None) -> None:
        self._process = process
        self._report = report

    async def run(self, cmd, cwd=None, env=None, new_process_group=False):  # type: ignore[no-untyped-def]
        if self._report is not None:
            prefix = "--json-report-file="
            report_arg = next(a for a in cmd if a.startswith(prefix))
            pathlib.Path(report_arg[len(prefix) :]).write_text(
                json.dumps(self._report), encoding="utf-8"
            )
        return self._process


class _ProjectInfoProvider:
    def __init__(self, project_dir: pathlib.Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._project_dir


def _context(payload: RunTestsRunPayload) -> RunTestsRunContext:
    return RunTestsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("exit_code", [2, 3, 4])
async def test_error_includes_stdout_and_stderr(
    tmp_path: pathlib.Path, exit_code: int
) -> None:
    (tmp_path / "tests").mkdir()
    handler = PytestRunTestsHandler(
        config=PytestRunTestsHandlerConfig(),
        logger=NoOpLogger(),
        command_runner=_CommandRunner(_Process(exit_code, _STDOUT, _STDERR)),  # type: ignore[arg-type]
        project_info_provider=_ProjectInfoProvider(tmp_path),  # type: ignore[arg-type]
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))

    message = str(exc_info.value)
    assert _STDOUT in message
    assert _STDERR in message


def _handler(
    project_dir: pathlib.Path, process: _Process, report: dict | None
) -> PytestRunTestsHandler:
    return PytestRunTestsHandler(
        config=PytestRunTestsHandlerConfig(),
        logger=NoOpLogger(),
        command_runner=_CommandRunner(process, report),  # type: ignore[arg-type]
        project_info_provider=_ProjectInfoProvider(project_dir),  # type: ignore[arg-type]
    )


_COLLECTION_ERROR_REPORT = {
    "exitcode": 2,
    "summary": {"total": 0, "collected": 8},
    "tests": [],
    "collectors": [
        {"nodeid": "", "outcome": "passed"},
        {
            "nodeid": "tests/test_broken.py",
            "outcome": "failed",
            "longrepr": _STDOUT,
        },
    ],
}


async def test_collection_error_is_returned_as_error_result(
    tmp_path: pathlib.Path,
) -> None:
    """Exit 2 caused by a failed collector is a test outcome, not an action
    failure: the module appears as an ERROR result carrying the traceback."""
    (tmp_path / "tests").mkdir()
    handler = _handler(
        tmp_path, _Process(2, "", _STDERR), _COLLECTION_ERROR_REPORT
    )

    result = await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))

    assert [(r.outcome, r.message) for r in result.test_results] == [
        (TestOutcome.ERROR, _STDOUT)
    ]
    assert result.test_results[0].test_id.file_path.endswith("tests/test_broken.py")


async def test_exit_2_without_failed_collector_still_fails(
    tmp_path: pathlib.Path,
) -> None:
    """Exit 2 with a report that shows no collection error (e.g. a keyboard
    interrupt) is a real interruption and stays an action failure."""
    (tmp_path / "tests").mkdir()
    report = {
        "exitcode": 2,
        "summary": {"total": 1, "collected": 1},
        "tests": [],
        "collectors": [{"nodeid": "", "outcome": "passed"}],
    }
    handler = _handler(tmp_path, _Process(2, _STDOUT, _STDERR), report)

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))

    assert _STDOUT in str(exc_info.value)
    assert _STDERR in str(exc_info.value)


def _test_entry(nodeid: str, outcome: str, **stages: dict) -> dict:
    return {"nodeid": nodeid, "lineno": 3, "outcome": outcome, **stages}


_EXPECTED_FAILURE_REPORT = {
    "exitcode": 0,
    "summary": {"total": 3, "collected": 3},
    "collectors": [{"nodeid": "", "outcome": "passed"}],
    "tests": [
        _test_entry(
            "tests/test_x.py::test_known_bug",
            "xfailed",
            call={
                "outcome": "skipped",
                "crash": {"path": "x", "lineno": 4, "message": "AssertionError: boom"},
                "longrepr": "def test_known_bug():\n>   assert False",
            },
        ),
        _test_entry("tests/test_x.py::test_fixed_bug", "xpassed"),
        _test_entry(
            "tests/test_x.py::test_strict_xpass",
            "failed",
            call={"outcome": "failed", "longrepr": "[XPASS(strict)] known bug"},
        ),
    ],
}


async def test_expected_failures_do_not_fail_the_run(tmp_path: pathlib.Path) -> None:
    """pytest treats xfail and a non-strict xpass as non-failing (exit 0); the
    results must too, while a strict xpass, reported by pytest as failed, still
    fails (issue #1004)."""
    (tmp_path / "tests").mkdir()
    handler = _handler(tmp_path, _Process(1, "", ""), _EXPECTED_FAILURE_REPORT)

    result = await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))

    by_name = {r.test_id.test_name: r for r in result.test_results}
    assert by_name["test_known_bug"].outcome == TestOutcome.SKIPPED
    assert by_name["test_known_bug"].message == (
        "expected failure (xfail): AssertionError: boom"
    )
    assert by_name["test_fixed_bug"].outcome == TestOutcome.PASSED
    assert by_name["test_strict_xpass"].outcome == TestOutcome.FAILED
    assert result.errors == 0


async def test_only_expected_failures_give_a_passing_run(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "tests").mkdir()
    report = dict(_EXPECTED_FAILURE_REPORT)
    report["tests"] = _EXPECTED_FAILURE_REPORT["tests"][:2]
    handler = _handler(tmp_path, _Process(0, "", ""), report)

    result = await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))

    assert result.return_code == code_action.RunReturnCode.SUCCESS


async def test_summary_shows_why_a_test_was_skipped(tmp_path: pathlib.Path) -> None:
    """An expected failure is listed under SKIPPED with its message, so the
    summary does not hide that it is a known failure."""
    (tmp_path / "tests").mkdir()
    handler = _handler(tmp_path, _Process(0, "", ""), _EXPECTED_FAILURE_REPORT)

    result = await handler.run(RunTestsRunPayload(), _context(RunTestsRunPayload()))
    text = result.to_text()
    plain = "".join(
        part if isinstance(part, str) else str(part["text"])
        for part in (text.text_parts if not isinstance(text, str) else [text])
    )

    assert "SKIPPED " in plain
    assert "expected failure (xfail): AssertionError: boom" in plain
