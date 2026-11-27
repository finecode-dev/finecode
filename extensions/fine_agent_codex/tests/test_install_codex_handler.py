"""The install-codex setup step reports skip, install, or failure correctly.

A broken install report leaves the operator believing Codex is ready when it is
not, so each outcome -- already present, fresh install, missing npm, failed npm
run, unstartable npm -- must land in exactly the right result list.
"""

from __future__ import annotations

import pathlib
import typing

import pytest
from fine_system_setup.setup_system_action import (
    SetupSystemAction,
    SetupSystemRunPayload,
    SetupSystemRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler

from fine_agent_codex import InstallCodexHandler, install_codex_handler


class _FakeProcess:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def wait_for_end(self, timeout: float | None = None) -> None:
        return None

    def get_exit_code(self) -> int | None:
        return self._exit_code

    def get_output(self) -> str:
        return self._stdout

    def get_error_output(self) -> str:
        return self._stderr


class _FakeCommandRunner:
    """Serves queued `(exit_code, stdout, stderr)` results, recording each call.

    An empty queue raises `AssertionError` so a test that forgets a response
    fails loudly instead of silently changing what is being exercised.
    """

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[
            tuple[list[str], pathlib.Path | None, dict[str, str] | None]
        ] = []

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> _FakeProcess:
        icommandrunner.check_argv(cmd)
        self.calls.append((list(cmd), cwd, env))
        assert self._responses, f"no queued response for command: {cmd}"
        exit_code, stdout, stderr = self._responses.pop(0)
        return _FakeProcess(exit_code, stdout, stderr)

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        return await self.run(
            cmd, cwd=cwd, env=env, new_process_group=new_process_group
        )

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise AssertionError("run_sync is not expected")


class _RaisingCommandRunner:
    """Raises the given exception from `run()`, recording the call."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[list[str]] = []

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> _FakeProcess:
        icommandrunner.check_argv(cmd)
        self.calls.append(list(cmd))
        raise self._error

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        return await self.run(
            cmd, cwd=cwd, env=env, new_process_group=new_process_group
        )

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise AssertionError("run_sync is not expected")


def _patch_which(
    monkeypatch: pytest.MonkeyPatch, codex: str | None, npm: str | None
) -> None:
    mapping = {"codex": codex, "npm": npm}

    def fake_which(name: str) -> str | None:
        return mapping.get(name)

    monkeypatch.setattr(install_codex_handler.shutil, "which", fake_which)


@pytest.mark.asyncio
async def test_present_codex_is_skipped_without_spawning_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Re-running setup where Codex is already installed changes nothing and spawns nothing.

    Without the skip, every setup run would reinstall the CLI over a working
    install and report a fresh install that never happened.
    """
    _patch_which(monkeypatch, codex="/fake/bin/codex", npm="/fake/bin/npm")
    runner = _FakeCommandRunner([])

    result = await run_handler(
        InstallCodexHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: runner},
    )

    assert isinstance(result, SetupSystemRunResult)
    assert result.installed == []
    assert result.skipped == ["codex"]
    assert result.failed == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_missing_codex_is_installed_with_a_single_global_npm_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A machine without Codex ends up with it installed after one npm invocation.

    The exact install command is the contract other tooling relies on; a wrong
    flag or package name here silently installs the wrong thing.
    """
    _patch_which(monkeypatch, codex=None, npm="/fake/bin/npm")
    runner = _FakeCommandRunner([(0, "", "")])

    result = await run_handler(
        InstallCodexHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: runner},
    )

    assert isinstance(result, SetupSystemRunResult)
    assert result.installed == ["codex"]
    assert result.skipped == []
    assert result.failed == []
    assert [list(cmd) for cmd, _cwd, _env in runner.calls] == [
        ["npm", "install", "-g", "--ignore-scripts", "@openai/codex"]
    ]


@pytest.mark.asyncio
async def test_missing_npm_fails_with_guidance_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Without npm the step fails with an actionable message instead of a shell error.

    Attempting the install anyway would surface as an opaque spawn failure; naming
    Node.js tells the operator exactly what to install first.
    """
    _patch_which(monkeypatch, codex=None, npm=None)
    runner = _FakeCommandRunner([])

    result = await run_handler(
        InstallCodexHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: runner},
    )

    assert isinstance(result, SetupSystemRunResult)
    assert result.failed == ["codex: npm not found in PATH, install Node.js first"]
    assert result.installed == []
    assert result.skipped == []
    assert result.return_code == code_action.RunReturnCode.ERROR
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "E404 boom\n", "codex: E404 boom"),
        ("out msg", "", "codex: out msg"),
    ],
)
async def test_failed_npm_run_reports_the_command_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    """A failed npm install surfaces npm's own error text in the result.

    An empty explanation would leave the operator with a red step and no hint
    whether the registry, the network, or permissions caused it.
    """
    _patch_which(monkeypatch, codex=None, npm="/fake/bin/npm")
    runner = _FakeCommandRunner([(1, stdout, stderr)])

    result = await run_handler(
        InstallCodexHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: runner},
    )

    assert isinstance(result, SetupSystemRunResult)
    assert result.failed == [expected]
    assert result.installed == []


@pytest.mark.asyncio
async def test_unstartable_npm_reports_which_program_could_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An npm that cannot even start is reported as a spawn failure naming npm.

    A vanished executable between the PATH check and the spawn would otherwise
    escape as an unhandled exception instead of a collected setup failure.
    """
    _patch_which(monkeypatch, codex=None, npm="/fake/bin/npm")
    runner = _RaisingCommandRunner(
        icommandrunner.ProgramStartError(
            "npm", None, FileNotFoundError(2, "No such file or directory")
        )
    )

    result = await run_handler(
        InstallCodexHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: runner},
    )

    assert isinstance(result, SetupSystemRunResult)
    assert result.failed == ["codex: could not start npm: No such file or directory"]
    assert result.installed == []
