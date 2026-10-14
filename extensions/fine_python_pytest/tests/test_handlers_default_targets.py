"""The pytest handlers skip spawning pytest when nothing could be collected,
and never skip when the payload names a target.

An empty project×env leg spawns pytest with no path arguments for every
project that lacks a `tests/` directory; process startup plus the fallback
discovery costs seconds per leg across dozens of such projects. The skip must
not hide real tests: any payload-named target, existing default dir, or
config sign reverts to spawning pytest.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fine_test.list_tests_action import (
    ListTestsRunContext,
    ListTestsRunPayload,
)
from fine_test.run_tests_action import RunTestsRunContext, RunTestsRunPayload
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger

from fine_python_pytest.list_tests_handler import (
    PytestListTestsHandler,
    PytestListTestsHandlerConfig,
)
from fine_python_pytest.run_tests_handler import (
    PytestRunTestsHandler,
    PytestRunTestsHandlerConfig,
)


class _FakeProcess:
    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self._exit_code = exit_code
        self._output = output

    def get_exit_code(self) -> int | None:
        return self._exit_code

    def get_output(self) -> str:
        return self._output

    def get_error_output(self) -> str:
        return ""

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self.get_output().splitlines():
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self.get_error_output().splitlines():
            yield line

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeSyncProcess:
    """ISyncProcess-shaped fake; `run_sync` is never exercised by these
    handlers."""

    def get_exit_code(self) -> int | None:
        return None

    def get_output(self) -> str:
        return ""

    def get_error_output(self) -> str:
        return ""

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeCommandRunner:
    """Captures every command string it is asked to run, instead of executing
    it."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.commands: list[str] = []
        self.cwds: list[pathlib.Path | None] = []
        self.envs: list[dict[str, str] | None] = []
        self.process_groups: list[bool] = []

    async def run(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> _FakeProcess:
        self.commands.append(cmd)
        self.cwds.append(cwd)
        self.envs.append(env)
        self.process_groups.append(new_process_group)
        return _FakeProcess(exit_code=self.exit_code, output="")

    def run_sync(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeSyncProcess:
        raise NotImplementedError


class _FakeProjectInfoProvider:
    """Only `get_current_project_dir_path` is exercised by the handlers."""

    def __init__(self, project_dir: pathlib.Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._project_dir

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_packages(
        self,
    ) -> dict[str, iprojectinfoprovider.WorkspacePackage]:
        raise NotImplementedError

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        raise NotImplementedError


def _make_run_handler(
    project_dir: pathlib.Path, command_runner: _FakeCommandRunner
) -> PytestRunTestsHandler:
    return PytestRunTestsHandler(
        config=PytestRunTestsHandlerConfig(),
        logger=NoOpLogger(),
        command_runner=command_runner,
        project_info_provider=_FakeProjectInfoProvider(project_dir),
    )


def _make_list_handler(
    project_dir: pathlib.Path, command_runner: _FakeCommandRunner
) -> PytestListTestsHandler:
    return PytestListTestsHandler(
        config=PytestListTestsHandlerConfig(),
        logger=NoOpLogger(),
        command_runner=command_runner,
        project_info_provider=_FakeProjectInfoProvider(project_dir),
    )


def _make_run_context(payload: RunTestsRunPayload) -> RunTestsRunContext:
    return RunTestsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )


def _make_list_context(payload: ListTestsRunPayload) -> ListTestsRunContext:
    return ListTestsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )


async def test_run_handler_skips_pytest_when_nothing_collectable(
    tmp_path: pathlib.Path,
) -> None:
    """A project with no `tests/`, no config, and no default-pattern file
    returns an empty result without spawning pytest."""
    command_runner = _FakeCommandRunner()
    result = await _make_run_handler(tmp_path, command_runner).run(
        RunTestsRunPayload(), _make_run_context(RunTestsRunPayload())
    )

    assert result.test_results == []
    assert command_runner.commands == []


async def test_run_handler_spawns_pytest_when_tests_dir_exists(
    tmp_path: pathlib.Path,
) -> None:
    """An existing `default_test_dirs` entry spawns pytest with that path as a
    positional argument. The fake exits 5 and writes no report, so the handler
    fails as it would with a real pytest that crashed before reporting."""
    (tmp_path / "tests").mkdir()
    command_runner = _FakeCommandRunner(exit_code=5)

    with pytest.raises(code_action.ActionFailedException):
        await _make_run_handler(tmp_path, command_runner).run(
            RunTestsRunPayload(), _make_run_context(RunTestsRunPayload())
        )

    assert len(command_runner.commands) == 1
    assert command_runner.commands[0][-1] == "tests"


async def test_list_handler_skips_pytest_when_nothing_collectable(
    tmp_path: pathlib.Path,
) -> None:
    """The list handler skips the same way: empty project, empty result, no
    pytest process."""
    command_runner = _FakeCommandRunner()
    result = await _make_list_handler(tmp_path, command_runner).run(
        ListTestsRunPayload(), _make_list_context(ListTestsRunPayload())
    )

    assert result.tests == []
    assert command_runner.commands == []


async def test_list_handler_spawns_pytest_when_tests_dir_exists(
    tmp_path: pathlib.Path,
) -> None:
    """An existing `tests/` spawns the collect run with `tests` as the last
    positional path; empty output means nothing was collected."""
    (tmp_path / "tests").mkdir()
    command_runner = _FakeCommandRunner()
    result = await _make_list_handler(tmp_path, command_runner).run(
        ListTestsRunPayload(), _make_list_context(ListTestsRunPayload())
    )

    assert result.tests == []
    assert len(command_runner.commands) == 1
    assert command_runner.commands[0][-1] == "tests"


async def test_run_handler_file_paths_never_skip(
    tmp_path: pathlib.Path,
) -> None:
    """A payload-named file path bypasses the skip even when it does not exist:
    pytest decides what its own arguments mean."""
    command_runner = _FakeCommandRunner(exit_code=5)
    payload = RunTestsRunPayload(
        file_paths=[path_to_resource_uri(tmp_path / "missing")]
    )

    with pytest.raises(code_action.ActionFailedException):
        await _make_run_handler(tmp_path, command_runner).run(
            payload, _make_run_context(payload)
        )

    assert len(command_runner.commands) == 1
