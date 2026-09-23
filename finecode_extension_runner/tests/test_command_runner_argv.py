"""`ICommandRunner` executes argv vectors directly — no shell on any OS.

The regression this file exists for: quoting conventions are per-shell and
per-platform, so a command built by joining strings under one rule can be
misparsed by another (cmd.exe on Windows was the observed failure). Under
exec, each argv element is one argument on every OS, so refusal replaces
quoting: the OS says what it can launch, and arguments no spawner can pass
safely are rejected with a typed error before any process starts.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from finecode_extension_api.interfaces import icommandrunner

from finecode_extension_runner.impls.command_runner import (
    _prepare_argv,
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.process_slots import ProcessSlots


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


def _runner() -> CommandRunner:
    return CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=4),
    )


def _runner_with_slots() -> tuple[CommandRunner, ProcessSlots]:
    slots = ProcessSlots(target=4)
    return (
        CommandRunner(
            logger=_NoopLogger(),
            config=CommandRunnerConfig(),
            process_slots=slots,
        ),
        slots,
    )


_ARGS = [
    "a b",
    "it's",
    'say "hi"',
    r"C:\x\y z",
    "%PATH%",
    "a&b|c<d>e^f(g)",
    "",
    "-m",
    "slow or not cli",
    "--json-report-file=C:\\t\\r.json",
]
"""cmd.exe's full nastiness: spaces, quotes, `%`, metacharacters, and an empty
argument — each must land at the program as exactly one argv element."""


async def test_arguments_arrive_verbatim() -> None:
    """The point of exec: nothing reinterpreted, nothing quoted away."""
    process = await _runner().run(
        [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *_ARGS]
    )
    await process.wait_for_end()

    assert json.loads(process.get_output()) == _ARGS


def test_run_sync_arguments_arrive_verbatim() -> None:
    process = _runner().run_sync(
        [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *_ARGS]
    )
    process.wait_for_end()

    assert json.loads(process.get_output()) == _ARGS


async def test_empty_argv_is_refused_without_taking_a_slot() -> None:
    runner, slots = _runner_with_slots()

    with pytest.raises(ValueError, match="must not be empty"):
        await runner.run([])

    assert slots.in_flight == 0


async def test_a_shell_string_is_refused_without_taking_a_slot() -> None:
    runner, slots = _runner_with_slots()

    with pytest.raises(TypeError, match="shell command string"):
        await runner.run("echo hi")
    assert slots.in_flight == 0

    with pytest.raises(TypeError, match="shell command string"):
        runner.run_sync("echo hi")
    assert slots.in_flight == 0


def test_check_argv_rejects_non_argv_shapes() -> None:
    with pytest.raises(TypeError):
        icommandrunner.check_argv("echo hi")
    with pytest.raises(TypeError):
        icommandrunner.check_argv(123)
    with pytest.raises(TypeError):
        icommandrunner.check_argv(["git", Path("status")])
    with pytest.raises(ValueError):
        icommandrunner.check_argv([])


async def test_missing_program_raises_and_releases_the_slot() -> None:
    runner, slots = _runner_with_slots()

    with pytest.raises(FileNotFoundError):
        await runner.run(["definitely-not-a-program-xyz"])

    assert slots.in_flight == 0


def test_bare_name_on_windows_is_resolved_through_pathext() -> None:
    def fake_which(program: str, path: str | None = None) -> str | None:
        assert program == "npm"
        return r"C:\tools\npm.CMD"

    argv = _prepare_argv(
        ["npm", "install"],
        env=None,
        platform="win32",
        which=fake_which,
    )
    assert argv == [r"C:\tools\npm.CMD", "install"]


@pytest.mark.parametrize(
    "unsafe", ['"', "%", "^", "&", "|", "<", ">", "!", "(", ")", "\r", "\n"]
)
def test_batch_argument_with_a_cmd_metacharacter_is_refused(unsafe: str) -> None:
    with pytest.raises(
        icommandrunner.UnsafeBatchArgumentError,
        match="argument 1",
    ) as excinfo:
        _prepare_argv(
            [r"C:\tools\npm.cmd", f"bad{unsafe}arg"],
            env=None,
            platform="win32",
            which=lambda *args, **kwargs: None,
        )
    assert r"C:\tools\npm.cmd" in str(excinfo.value)


def test_quoted_batch_path_may_contain_parentheses() -> None:
    """A space makes list2cmdline quote the path; cmd tolerates `( )` inside
    quotes, so only the always-unsafe set is refused then."""
    path = r"C:\Program Files (x86)\nodejs\npm.cmd"
    argv = _prepare_argv(
        [path, "install"],
        env=None,
        platform="win32",
        which=lambda *args, **kwargs: None,
    )
    assert argv[0] == path


@pytest.mark.parametrize("metachar", ['"', "%", "\r", "\n"])
def test_batch_path_with_an_expand_or_terminate_char_is_always_refused(
    metachar: str,
) -> None:
    with pytest.raises(icommandrunner.UnsafeBatchArgumentError, match="argument 0"):
        _prepare_argv(
            [f"C:\\tmp\\p{metachar}.cmd"],
            env=None,
            platform="win32",
            which=lambda *args, **kwargs: None,
        )


def test_unquoted_batch_path_metachar_is_refused() -> None:
    with pytest.raises(icommandrunner.UnsafeBatchArgumentError, match="argument 0"):
        _prepare_argv(
            [r"C:\a&b\npm.cmd"],
            env=None,
            platform="win32",
            which=lambda *args, **kwargs: None,
        )


def test_argv_with_a_path_separator_is_not_resolved() -> None:
    def explode(*args: object, **kwargs: object) -> str | None:
        raise AssertionError("which must not be called for a path argv[0]")

    argv = _prepare_argv(
        [r"C:\tools\npm", "install"],
        env=None,
        platform="win32",
        which=explode,
    )
    assert argv == [r"C:\tools\npm", "install"]


def test_unresolved_bare_name_is_passed_through_unchanged() -> None:
    argv = _prepare_argv(
        ["npm", "install"],
        env=None,
        platform="win32",
        which=lambda *args, **kwargs: None,
    )
    assert argv == ["npm", "install"]


@pytest.mark.parametrize("suffix", [".js", ".py", ".vbs"])
def test_non_launchable_resolution_is_refused(suffix: str) -> None:
    resolved = f"C:\\tools\\npm{suffix}"

    with pytest.raises(icommandrunner.UnlaunchableProgramError) as excinfo:
        _prepare_argv(
            ["npm", "install"],
            env=None,
            platform="win32",
            which=lambda program, path=None: resolved,
        )

    message = str(excinfo.value)
    assert resolved in message
    assert re.search(r"expected one of .*\.exe.*\.cmd.*\.bat", message)


def test_path_is_read_case_insensitively_from_env() -> None:
    seen: list[str | None] = []

    def fake_which(program: str, path: str | None = None) -> str | None:
        seen.append(path)
        return None

    _prepare_argv(
        ["npm", "install"],
        env={"Path": r"C:\p"},
        platform="win32",
        which=fake_which,
    )
    assert seen == [r"C:\p"]


def test_non_windows_platform_returns_argv_unchanged() -> None:
    def explode(*args: object, **kwargs: object) -> str | None:
        raise AssertionError("which must not be called off win32")

    argv = _prepare_argv(
        ["npm", "install"],
        env=None,
        platform="linux",
        which=explode,
    )
    assert argv == ["npm", "install"]