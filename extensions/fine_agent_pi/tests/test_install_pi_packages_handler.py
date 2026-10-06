from __future__ import annotations

import json
import os
import pathlib
from typing import Any

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

from fine_agent_pi import install_pi_packages_handler
from fine_agent_pi.install_pi_packages_handler import InstallPiPackagesHandler

A = "npm:a@1.0.0"
B = "npm:b@1.0.0"


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

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise AssertionError("run_sync is not expected")


class _InstallRefusingCommandRunner:
    """Serves `pi list` responses but refuses any `pi install` with
    `UnsafeBatchArgumentError`, as a `.cmd` shim would for an argument cmd.exe
    would reinterpret."""

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
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
        if cmd[0:2] == ["pi", "install"] and cmd[2] == A:
            raise icommandrunner.UnsafeBatchArgumentError(
                "argument 2 cannot be passed safely to batch file pi.cmd"
            )
        assert self._responses, f"no queued response for command: {cmd}"
        exit_code, stdout, stderr = self._responses.pop(0)
        return _FakeProcess(exit_code, stdout, stderr)

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise AssertionError("run_sync is not expected")


@pytest.fixture
def pi_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = install_pi_packages_handler.shutil.which

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/fake/bin/pi"
        return real_which(name)

    monkeypatch.setattr(install_pi_packages_handler.shutil, "which", fake_which)


@pytest.fixture
def pi_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = install_pi_packages_handler.shutil.which

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return None
        return real_which(name)

    monkeypatch.setattr(install_pi_packages_handler.shutil, "which", fake_which)


async def _run(
    runner: _FakeCommandRunner,
    packages: list[str],
    *,
    project_dir: pathlib.Path,
    **config: Any,
) -> SetupSystemRunResult:
    result = await run_handler(
        InstallPiPackagesHandler,
        SetupSystemRunPayload(),
        action_cls=SetupSystemAction,
        project_dir=project_dir,
        service_overrides={ICommandRunner: runner},
        handler_config={"packages": packages, **config},
    )
    assert isinstance(result, SetupSystemRunResult)
    return result


def _listing(
    tmp_path: pathlib.Path,
    *entries: str | tuple[str, str],
) -> str:
    """A `pi list` project block whose entries point at real installed dirs.

    A bare entry takes its version from the spec's pin; a `(src, version)` entry
    overrides it, which is how drift from the configured pin is set up.
    """
    lines = ["Project packages:"]
    for entry in entries:
        if isinstance(entry, tuple):
            src, version = entry
        else:
            src, version = entry, None
        split = install_pi_packages_handler._split_npm_spec(src)
        assert split is not None, src
        name, pinned_version = split
        if version is None:
            version = pinned_version
        path = tmp_path / ".pi" / "npm" / "node_modules" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(json.dumps({"version": version}))
        lines.append(f"  {src}")
        lines.append(f"    {path}")
    return "\n".join(lines)


def _commands(runner: _FakeCommandRunner) -> list[list[str]]:
    return [cmd for cmd, _cwd, _env in runner.calls]


@pytest.mark.asyncio
async def test_each_configured_package_is_installed_into_the_project_when_none_is_present(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A clean project gets one `pi install` per configured package, in config order,
    and both are reported installed only after a record check confirms them.

    If the handler skipped the record check an install pi silently discarded would be
    reported as done, and the operator would learn about it only when pi never loaded
    the package.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A, B), ""),
        ]
    )

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert result.installed == [f"pi package {A}", f"pi package {B}"]
    assert result.skipped == []
    assert result.failed == []
    assert _commands(runner) == [
        ["pi", "list", "--approve"],
        ["pi", "install", A, "-l", "--approve"],
        ["pi", "install", B, "-l", "--approve"],
        ["pi", "list", "--approve"],
    ]


@pytest.mark.asyncio
async def test_a_second_run_skips_every_present_package_and_installs_nothing(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """Re-running setup on a project that already has the packages must spawn only the
    initial `pi list`.

    `pi install` is not a no-op -- it runs npm every time -- so a missing skip turns
    every setup into a network round trip and a false `installed` report.
    """
    runner = _FakeCommandRunner([(0, _listing(tmp_path, A, B), "")])

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert result.skipped == [f"pi package {A}", f"pi package {B}"]
    assert result.installed == []
    assert result.failed == []
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_a_package_in_settings_but_missing_on_disk_is_reinstalled(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A configured source pi lists without an installed path is treated as absent and
    installed again.

    Trusting the entry alone would report success for a package whose files were
    deleted; pi would then discover the gap at the next session.
    """
    runner = _FakeCommandRunner(
        [
            (0, "Project packages:\n  npm:a@1.0.0", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.installed == [f"pi package {A}"]
    assert result.failed == []


@pytest.mark.asyncio
async def test_a_different_source_string_in_settings_is_reinstalled_to_the_configured_pin(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A settings entry pinned to another version is replaced with the configured pin.

    The config is authoritative for pins; leaving the older source in place would let
    the developer keep running a version the config no longer names.
    """
    runner = _FakeCommandRunner(
        [
            (0, _listing(tmp_path, "npm:a@0.9.0"), ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.installed == [f"pi package {A}"]
    install_commands = [
        cmd for cmd in _commands(runner) if cmd[0:2] == ["pi", "install"]
    ]
    assert install_commands == [["pi", "install", A, "-l", "--approve"]]


async def test_a_batch_shim_refusing_one_install_argument_fails_only_that_package(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A Windows `.cmd` shim refusal is per-package, not per-run.

    On Windows a resolved `pi.cmd` runs through cmd.exe, and arguments it would
    reinterpret are refused structurally: that package lands in `failed` while
    the others still install -- the same continuation policy as a nonzero exit.
    """
    runner = _InstallRefusingCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, B), ""),
        ]
    )

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert result.failed == [
        f"pi package {A}: could not start pi: "
        "argument 2 cannot be passed safely to batch file pi.cmd"
    ]
    assert result.installed == [f"pi package {B}"]


@pytest.mark.asyncio
async def test_an_exact_pin_whose_installed_version_differs_is_reinstalled(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """An exact pin is re-installed when the on-disk `package.json` version differs.

    This mirrors pi's own startup check: without it a pin the config changed would
    never take effect on a machine that already had an older build on disk.
    """
    runner = _FakeCommandRunner(
        [
            (0, _listing(tmp_path, (A, "0.9.0")), ""),
            (0, "", ""),
            (0, _listing(tmp_path, (A, "0.9.0")), ""),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.installed == [f"pi package {A}"]


@pytest.mark.asyncio
async def test_a_range_spec_is_checked_for_existence_only(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A range spec is skipped whenever an installed path exists, regardless of version.

    Resolving whether an installed version satisfies the range is pi's job; a handler
    that guessed would risk reinstalling on every run.
    """
    src = "npm:c@^1.0.0"
    runner = _FakeCommandRunner([(0, _listing(tmp_path, (src, "1.4.0")), "")])

    result = await _run(runner, [src], project_dir=tmp_path)

    assert result.skipped == [f"pi package {src}"]
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_a_failing_source_is_reported_with_pi_stderr_tail_and_the_rest_still_install(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """One failed install names the last lines of pi's stderr and does not stop the
    others; the run's return code is ERROR.

    npm inherits pi's stdio, so the result has to carry the tail while the full log
    stays in the handler log -- otherwise the operator sees either nothing or an
    entire npm transcript in the summary.
    """
    stderr_lines = [
        "npm ERR! line one",
        "npm ERR! line two",
        "npm ERR! line three",
        "npm ERR! line four",
        "npm ERR! line five",
        "npm ERR! line six",
        "npm ERR! line seven",
        f"Error: npm install {A} failed with code 1",
    ]
    expected_tail = "\n".join(stderr_lines[-5:])
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (1, "", "\n".join(stderr_lines)),
            (0, "", ""),
            (0, _listing(tmp_path, B), ""),
        ]
    )

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert result.failed == [f"pi package {A}: {expected_tail}"]
    assert result.installed == [f"pi package {B}"]
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_a_failure_with_empty_stderr_reports_stdout(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """When a failed command wrote nothing to stderr, its stdout is reported instead.

    A command that fails without a stderr message would otherwise produce an empty
    explanation, leaving the operator nothing to act on.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (1, "boom", ""),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.failed == [f"pi package {A}: boom"]
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_error_text_is_stripped_of_ansi_colour(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """Colour codes from a terminal-styled pi never reach the result strings.

    Embedded escape sequences make the reported error unreadable in logs and
    terminals that do not interpret them.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (1, "", "\x1b[31mError: x\x1b[39m"),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.failed == [f"pi package {A}: Error: x"]


@pytest.mark.asyncio
async def test_missing_pi_fails_every_package_without_spawning_anything(
    pi_missing: None, tmp_path: pathlib.Path
) -> None:
    """With no `pi` on PATH every package is failed with an actionable message and no
    subprocess is spawned.

    Attempting the command anyway would surface as an opaque shell error instead of
    telling the operator to run `install_pi` first.
    """
    runner = _FakeCommandRunner([])

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert len(result.failed) == 2
    assert all("install pi first" in entry for entry in result.failed)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_empty_package_list_reports_nothing_and_spawns_nothing(
    pi_missing: None, tmp_path: pathlib.Path
) -> None:
    """No configured packages is a clean no-op: no result entries and no subprocess,
    even when `pi` is absent.

    The empty default config is the common case for every project that never opts in,
    so it must stay free of cost and free of noise.
    """
    runner = _FakeCommandRunner([])

    result = await _run(runner, [], project_dir=tmp_path)

    assert result.installed == result.skipped == result.failed == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_install_env_ignores_lifecycle_scripts_and_keeps_the_rest_of_the_environment(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """By default the child environment disables npm lifecycle scripts and retains the
    parent's variables.

    Replacing the environment wholesale would drop PATH and break npm; running the
    scripts would execute arbitrary install-time code from the dependency tree.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    await _run(runner, [A], project_dir=tmp_path)

    assert len(runner.calls) == 3
    for _cmd, _cwd, env in runner.calls:
        assert env is not None
        assert env["npm_config_ignore_scripts"] == "true"
        assert env["PATH"] == os.environ["PATH"]


@pytest.mark.asyncio
async def test_allowing_lifecycle_scripts_inherits_the_environment_unchanged(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """The opt-out passes no environment override at all.

    A package whose install genuinely depends on a lifecycle script needs the real
    inherited environment, not one rebuilt by the handler.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    await _run(runner, [A], project_dir=tmp_path, allow_lifecycle_scripts=True)

    for _cmd, _cwd, env in runner.calls:
        assert env is None


@pytest.mark.asyncio
async def test_a_local_path_source_is_refused_and_the_others_proceed(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A local-path source is failed as unsupported while the remaining packages still
    install.

    pi stores a local source relative to the settings file, so it has no meaning in
    config that is shared or copied between machines.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    result = await _run(runner, ["./pkg", A], project_dir=tmp_path)

    assert len(result.failed) == 1
    assert result.failed[0].startswith("pi package ./pkg: unsupported source")
    assert result.installed == [f"pi package {A}"]
    assert not any("./pkg" in cmd for cmd in _commands(runner))


@pytest.mark.asyncio
async def test_remote_prefixes_match_case_insensitively(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """An uppercase remote prefix is accepted rather than refused.

    Source detection follows pi's own case-insensitive parsing, so a valid source is
    never misclassified as a local path because of letter case.
    """
    src = "HTTPS://github.com/u/r@v1"
    installed_dir = tmp_path / ".pi" / "git" / "github.com" / "u" / "r"
    installed_dir.mkdir(parents=True)
    listing = f"Project packages:\n  {src}\n    {installed_dir}"
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, listing, ""),
        ]
    )

    result = await _run(runner, [src], project_dir=tmp_path)

    assert result.installed == [f"pi package {src}"]
    assert ["pi", "install", src, "-l", "--approve"] in _commands(runner)


@pytest.mark.asyncio
async def test_an_unlistable_pi_fails_every_supported_package_and_installs_nothing(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A failing `pi list` fails every package and stops before any install.

    Installing without knowing what is already present is how a partial state is
    created; refusing keeps the outcome a visible failure rather than a guess.
    """
    runner = _FakeCommandRunner([(1, "", "list exploded")])

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert len(result.failed) == 2
    assert all(
        "could not list installed pi packages" in entry for entry in result.failed
    )
    assert result.installed == []
    assert len(runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "global"])
async def test_unreadable_pi_settings_fail_every_package_and_install_nothing(
    pi_on_path: None, tmp_path: pathlib.Path, scope: str
) -> None:
    """A settings warning from pi in either scope fails every package without installing.

    pi can exit 0 and print `Installed` while recording nothing (project settings) or
    while running under a settings view that has lost the developer's global
    configuration (global settings); the guard turns that silent corruption into a
    refusal.
    """
    stderr = (
        f"Warning (package command, {scope} settings): Unexpected end of JSON input\n"
        "    at JSON.parse (<anonymous>)"
    )
    runner = _FakeCommandRunner([(0, "No packages installed.", stderr)])

    result = await _run(runner, [A, B], project_dir=tmp_path)

    assert len(result.failed) == 2
    for entry in result.failed:
        assert "pi settings are unreadable" in entry
        assert f"{scope} settings" in entry
        assert "at JSON.parse" not in entry
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_commands_run_in_the_project_directory(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """Every command, including the record check, runs with the project dir as cwd.

    pi resolves project packages from its cwd, so a command run elsewhere would write
    to a different `.pi/` than the one the operator registered.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    await _run(runner, [A], project_dir=tmp_path)

    assert len(runner.calls) == 3
    assert all(cwd == tmp_path for _cmd, cwd, _env in runner.calls)


@pytest.mark.asyncio
async def test_duplicate_sources_install_once(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A source repeated in config is installed and reported exactly once.

    Repeating it would otherwise install the same package twice and report it twice,
    inflating the result without changing the outcome.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    result = await _run(runner, [A, A], project_dir=tmp_path)

    assert result.installed == [f"pi package {A}"]
    assert [cmd for cmd in _commands(runner) if cmd[0:2] == ["pi", "install"]] == [
        ["pi", "install", A, "-l", "--approve"]
    ]


def test_parser_returns_nothing_for_no_packages() -> None:
    """The empty listing pi prints with no packages yields no entries.

    Treating the message as a source would invent a package that is not configured.
    """
    assert (
        install_pi_packages_handler._parse_project_packages("No packages installed.")
        == {}
    )


def test_parser_records_a_valid_path_only_for_an_existing_absolute_directory(
    tmp_path: pathlib.Path,
) -> None:
    """Only an absolute existing directory counts as an installed path.

    A relative or nonexistent path must not satisfy presence, or the handler would
    skip a package that is not actually installed.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    output = (
        f"Project packages:\n  {A}\n    {real_dir}\n"
        f"  npm:b@1.0.0\n    {tmp_path / 'nope'}"
    )

    assert install_pi_packages_handler._parse_project_packages(output) == {
        A: str(real_dir),
        "npm:b@1.0.0": None,
    }


def test_parser_treats_a_non_path_indented_line_as_missing() -> None:
    """A four-space line that is not an absolute directory leaves the entry present
    but pathless.

    This is the false-skip guard: an unrecognised line can never be mistaken for a
    source or for a valid installation.
    """
    output = f"Project packages:\n  {A}\n    (missing)"

    assert install_pi_packages_handler._parse_project_packages(output) == {A: None}


def test_parser_ignores_user_section_entries(tmp_path: pathlib.Path) -> None:
    """A user-scope entry is not counted as present for the project.

    The project is the install target; counting a user entry would skip the project
    install and leave the project without the package in the sessions that matter.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    output = (
        f"User packages:\n  npm:u@1.0.0\n\nProject packages:\n  {A}\n    {real_dir}"
    )

    assert install_pi_packages_handler._parse_project_packages(output) == {
        A: str(real_dir)
    }


def test_parser_strips_the_filtered_marker_and_ansi_colour(
    tmp_path: pathlib.Path,
) -> None:
    """The `(filtered)` marker and terminal colour codes are removed before a source
    is recorded.

    Leaving them on would make the configured source string never match what pi
    lists, causing a reinstall on every run.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    output = (
        "\x1b[32mProject packages:\x1b[39m\n"
        f"  \x1b[36m{A} (filtered)\x1b[39m\n"
        f"    {real_dir}"
    )

    assert install_pi_packages_handler._parse_project_packages(output) == {
        A: str(real_dir)
    }


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("npm:@ff-labs/pi-fff@0.10.6", ("@ff-labs/pi-fff", "0.10.6")),
        ("npm:pi-clear@0.1.1", ("pi-clear", "0.1.1")),
    ],
)
def test_exact_pin_parses_scoped_and_unscoped_names(
    src: str, expected: tuple[str, str]
) -> None:
    """A scoped npm name and an unscoped one both split into name and version.

    Mis-splitting a scoped name would compare the wrong `package.json` version and
    produce a permanent reinstall.
    """
    assert install_pi_packages_handler._exact_npm_pin(src) == expected


@pytest.mark.parametrize(
    "src",
    ["npm:a", "npm:a@^1.0.0", "npm:@s/a", "git:github.com/u/r@v1"],
)
def test_ranges_and_unpinned_specs_are_not_exact_pins(src: str) -> None:
    """Only a full semver pin is treated as exact; ranges, unpinned specs and non-npm
    sources are not.

    Claiming exactness for a range would let the handler skip a package whose
    installed version the range does not actually satisfy.
    """
    assert install_pi_packages_handler._exact_npm_pin(src) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_recorded", "not_on_disk"])
async def test_an_install_pi_reports_but_does_not_leave_present_is_failed(
    pi_on_path: None, tmp_path: pathlib.Path, case: str
) -> None:
    """An install pi exits 0 on is reported failed unless the record check confirms it.

    pi can print `Installed` while writing nothing (a read-only settings file) or while
    leaving no files on disk (a custom npm command); without this check the operator
    would be told a package is installed that is not.
    """
    if case == "not_recorded":
        record_output = "No packages installed."
        needle = "did not record it"
    else:
        record_output = f"Project packages:\n  {A}"
        needle = "not installed on disk"
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, record_output, ""),
        ]
    )

    result = await _run(runner, [A], project_dir=tmp_path)

    assert result.installed == []
    assert len(result.failed) == 1
    assert needle in result.failed[0]
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_two_specs_of_one_npm_package_fail_the_later_one(
    pi_on_path: None, tmp_path: pathlib.Path
) -> None:
    """A second npm spec of an already-configured package is failed, and only the
    first is installed.

    pi replaces a same-identity entry rather than keeping both, so the two specs would
    fight on every run and idempotency would be lost.
    """
    runner = _FakeCommandRunner(
        [
            (0, "No packages installed.", ""),
            (0, "", ""),
            (0, _listing(tmp_path, A), ""),
        ]
    )

    result = await _run(runner, [A, "npm:a@2.0.0"], project_dir=tmp_path)

    assert result.failed == [
        f"pi package npm:a@2.0.0: same package as {A}; configure one version per package"
    ]
    assert [cmd for cmd in _commands(runner) if cmd[0:2] == ["pi", "install"]] == [
        ["pi", "install", A, "-l", "--approve"]
    ]
