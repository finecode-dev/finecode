from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from finecode_extension_api.interfaces import icommandrunner, ilogger
from finecode_extension_runner._services.run_action import (
    ActionFailedException as ActionRunFailed,
)
from finecode_extension_runner.testing import NoOpLogger, run_handler

from fine_python_lang.list_obtainable_python_interpreters_action import (
    ListObtainablePythonInterpretersAction,
    ListObtainablePythonInterpretersRunPayload,
)
from fine_python_uv.list_obtainable_python_interpreters_handler import (
    UvListObtainablePythonInterpretersHandler,
)

# These tests pin the handler's *logic* against controlled input: the paths real uv
# cannot be made to produce on demand (a prerelease, a freethreaded variant, malformed
# output). The companion integration test guards the other half — that this assumed
# JSON shape still matches what real uv emits.
#
# A faithful slice of `uv python list --only-downloads --all-versions
# --output-format json`, trimmed to the fields the handler reads. It deliberately
# contains every shape the handler must collapse or drop: a stable prerelease
# (3.15.0b1), a freethreaded variant of it, two patch levels of one minor (3.14.5 and
# 3.14.4, which must fold to a single cpython@3.14), and three implementations.
_UV_JSON = """
[
  {"key": "cpython-3.15.0b1-linux-x86_64-gnu", "version": "3.15.0b1",
   "version_parts": {"major": 3, "minor": 15, "patch": 0},
   "implementation": "cpython", "os": "linux", "variant": "default"},
  {"key": "cpython-3.15.0b1+freethreaded-linux-x86_64-gnu", "version": "3.15.0b1",
   "version_parts": {"major": 3, "minor": 15, "patch": 0},
   "implementation": "cpython", "os": "linux", "variant": "freethreaded"},
  {"key": "cpython-3.14.5-linux-x86_64-gnu", "version": "3.14.5",
   "version_parts": {"major": 3, "minor": 14, "patch": 5},
   "implementation": "cpython", "os": "linux", "variant": "default"},
  {"key": "cpython-3.14.4-linux-x86_64-gnu", "version": "3.14.4",
   "version_parts": {"major": 3, "minor": 14, "patch": 4},
   "implementation": "cpython", "os": "linux", "variant": "default"},
  {"key": "cpython-3.11.15-linux-x86_64-gnu", "version": "3.11.15",
   "version_parts": {"major": 3, "minor": 11, "patch": 15},
   "implementation": "cpython", "os": "linux", "variant": "default"},
  {"key": "pypy-3.11.15-linux-x86_64-gnu", "version": "3.11.15",
   "version_parts": {"major": 3, "minor": 11, "patch": 15},
   "implementation": "pypy", "os": "linux", "variant": "default"},
  {"key": "graalpy-3.12.0-linux-x86_64-gnu", "version": "3.12.0",
   "version_parts": {"major": 3, "minor": 12, "patch": 0},
   "implementation": "graalpy", "os": "linux", "variant": "default"}
]
"""


class _FakeProcess:
    def __init__(self, output: str = "", error_output: str = "", exit_code: int = 0):
        self._output = output
        self._error_output = error_output
        self._exit_code = exit_code

    def get_exit_code(self) -> int | None:
        return self._exit_code

    def get_output(self) -> str:
        return self._output

    def get_error_output(self) -> str:
        return self._error_output

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeCommandRunner:
    """Returns a fixed process for `run`, and records the command it was given."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self.commands: list[str] = []

    async def run(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        self.commands.append(cmd)
        return self._process

    def run_sync(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise NotImplementedError


def _overrides(command_runner: _FakeCommandRunner) -> dict[type, Any]:
    return {
        icommandrunner.ICommandRunner: command_runner,
        ilogger.ILogger: NoOpLogger(),
    }


async def _run(
    command_runner: _FakeCommandRunner,
    payload: ListObtainablePythonInterpretersRunPayload | None = None,
    handler_config: dict | None = None,
):
    return await run_handler(
        UvListObtainablePythonInterpretersHandler,
        payload or ListObtainablePythonInterpretersRunPayload(),
        action_cls=ListObtainablePythonInterpretersAction,
        service_overrides=_overrides(command_runner),
        handler_config=handler_config,
    )


async def test_collapses_patches_drops_prereleases_and_variants() -> None:
    command_runner = _FakeCommandRunner(_FakeProcess(output=_UV_JSON))

    result = await _run(command_runner)

    # 3.14.5 and 3.14.4 fold to one; 3.15.0b1 (default and freethreaded) is gone;
    # sorted by implementation then version
    assert result is not None
    assert result.toolchains == [
        "cpython@3.11",
        "cpython@3.14",
        "graalpy@3.12",
        "pypy@3.11",
    ]


async def test_uses_only_downloads_not_installed_state() -> None:
    command_runner = _FakeCommandRunner(_FakeProcess(output=_UV_JSON))

    await _run(command_runner)

    assert len(command_runner.commands) == 1
    command = command_runner.commands[0]
    # the determinism guarantee: uv's manifest, not the machine's installed pythons
    assert "--only-downloads" in command
    assert "--output-format json" in command


async def test_include_prereleases_admits_the_beta() -> None:
    command_runner = _FakeCommandRunner(_FakeProcess(output=_UV_JSON))

    result = await _run(
        command_runner,
        payload=ListObtainablePythonInterpretersRunPayload(include_prereleases=True),
    )

    # the default-variant beta appears; the freethreaded one still does not
    assert result is not None
    assert result.toolchains.count("cpython@3.15") == 1


async def test_variant_config_selects_freethreaded_builds() -> None:
    command_runner = _FakeCommandRunner(_FakeProcess(output=_UV_JSON))

    result = await _run(
        command_runner,
        payload=ListObtainablePythonInterpretersRunPayload(include_prereleases=True),
        handler_config={"variant": "freethreaded"},
    )

    # only the freethreaded row remains; the default builds are filtered out
    assert result is not None
    assert result.toolchains == ["cpython@3.15"]


async def test_nonzero_exit_is_an_error() -> None:
    command_runner = _FakeCommandRunner(
        _FakeProcess(error_output="uv exploded", exit_code=1)
    )

    with pytest.raises(ActionRunFailed, match="uv exploded"):
        await _run(command_runner)


async def test_entry_missing_version_parts_is_skipped_not_fatal() -> None:
    # regression: `version_parts` and `implementation` were read outside the try that
    # guards `version`, so one drifted row raised a raw KeyError out of run() instead of
    # being skipped like any other unreadable entry
    entries = json.loads(_UV_JSON)
    entries.append(
        {
            "key": "cpython-3.13.1-linux-x86_64-gnu",
            "version": "3.13.1",
            "implementation": "cpython",
            "os": "linux",
            "variant": "default",
        }
    )
    command_runner = _FakeCommandRunner(_FakeProcess(output=json.dumps(entries)))

    result = await _run(command_runner)

    assert result is not None
    # the good rows still came through; only the drifted one is gone
    assert result.toolchains == [
        "cpython@3.11",
        "cpython@3.14",
        "graalpy@3.12",
        "pypy@3.11",
    ]


async def test_every_entry_unreadable_is_an_error() -> None:
    # tolerating one bad row is right; reading none of them is schema drift, and must
    # not degrade to an empty axis that only surfaces far downstream as
    # "requires-python matches no obtainable CPython version"
    entries = [
        {"key": "cpython-3.14.5", "variant": "default", "ver": "3.14.5"},
        {"key": "cpython-3.13.1", "variant": "default", "ver": "3.13.1"},
    ]
    command_runner = _FakeCommandRunner(_FakeProcess(output=json.dumps(entries)))

    with pytest.raises(ActionRunFailed, match="output format has likely changed"):
        await _run(command_runner)


async def test_legitimately_empty_result_is_not_an_error() -> None:
    # the drift guard keys on parse failures, not on an empty result: a variant with no
    # builds is a normal answer, and must not be reported as uv changing its format
    command_runner = _FakeCommandRunner(_FakeProcess(output=_UV_JSON))

    result = await _run(command_runner, handler_config={"variant": "freethreaded"})

    assert result is not None
    # the only freethreaded row in the fixture is a prerelease, excluded by default
    assert result.toolchains == []


async def test_unparsable_output_is_an_error() -> None:
    command_runner = _FakeCommandRunner(_FakeProcess(output="not json at all"))

    with pytest.raises(ActionRunFailed, match="parse"):
        await _run(command_runner)
