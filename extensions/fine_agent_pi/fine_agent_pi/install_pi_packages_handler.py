import dataclasses
import json
import os
import pathlib
import re
import shutil

from fine_agent import backend_support
from fine_system_setup.setup_system_action import (
    SetupSystemAction,
    SetupSystemRunContext,
    SetupSystemRunPayload,
    SetupSystemRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)

_REMOTE_PREFIXES = ("npm:", "git:", "https://", "http://", "ssh://", "git://")
"""Source prefixes pi installs from a remote. Matched case-insensitively; anything
else pi resolves relative to the settings file, so it has no machine-independent
meaning in shared config."""

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_FILTERED_SUFFIX = " (filtered)"
_SETTINGS_WARNING = re.compile(
    r"Warning \(package command, (?:global|project) settings\): "
)
_EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_ERROR_TAIL_LINES = 5


@dataclasses.dataclass
class InstallPiPackagesHandlerConfig(code_action.ActionHandlerConfig):
    packages: list[str] = dataclasses.field(default_factory=list)
    allow_lifecycle_scripts: bool = False


def _error_text(process: icommandrunner.IAsyncProcess) -> tuple[str, str]:
    """Return pi's output for a failed command as `(full, short)`.

    npm inherits pi's stdio, so `full` can be a long npm log; `short` keeps only
    the tail that carries pi's own final `Error:` line for the result entry.
    """
    stderr = process.get_error_output()
    raw = stderr if stderr.strip() else process.get_output()
    full = _ANSI_ESCAPE.sub("", raw).strip()
    short = "\n".join(
        [line for line in full.splitlines() if line.strip()][-_ERROR_TAIL_LINES:]
    )
    return full, short


def _parse_project_packages(output: str) -> dict[str, str | None]:
    """Map each project-scope source in `pi list` output to its installed path.

    A value of `None` means the source is recorded but no valid installed path
    follows it: either the entry is missing on disk, or the line is not an
    absolute existing directory (e.g. `(missing)` or a custom `npmCommand` that
    did not install). Rules are tried in order and the first match wins.
    """
    result: dict[str, str | None] = {}
    section: str | None = None
    last: str | None = None
    for line in _ANSI_ESCAPE.sub("", output).splitlines():
        if line == "Project packages:":
            section = "project"
            last = None
            continue
        if line == "User packages:":
            section = "user"
            last = None
            continue
        if section != "project":
            continue
        if line.startswith("    "):
            if last is None:
                continue
            path = line.strip()
            if os.path.isabs(path) and pathlib.Path(path).is_dir():
                result[last] = path
            continue
        if line.startswith("  ") and line.strip():
            src = line.strip().removesuffix(_FILTERED_SUFFIX)
            result[src] = None
            last = src
            continue
    return result


def _split_npm_spec(src: str) -> tuple[str, str | None] | None:
    """Split an `npm:` source into `(name, version_or_None)`, else `None`."""
    if not src.lower().startswith("npm:"):
        return None
    spec = src[4:]
    idx = spec.find("@", 1) if spec.startswith("@") else spec.find("@")
    if idx == -1:
        return (spec, None)
    return (spec[:idx], spec[idx + 1 :])


def _exact_npm_pin(src: str) -> tuple[str, str] | None:
    """Return `(name, version)` when `src` is an exact npm pin, else `None`.

    A range (`^1.0.0`) or an unpinned spec has no version we can compare against
    the installed `package.json`, so presence for it is checked by existence
    only.
    """
    split = _split_npm_spec(src)
    if split is None:
        return None
    name, version = split
    if version is None or _EXACT_VERSION.fullmatch(version) is None:
        return None
    return (name, version)


def _installed_version_matches(path: str, version: str) -> bool:
    try:
        data = json.loads((pathlib.Path(path) / "package.json").read_text())
    except (OSError, ValueError):
        return False
    return data.get("version") == version


class InstallPiPackagesHandler(
    code_action.ActionHandler[
        SetupSystemAction,
        InstallPiPackagesHandlerConfig,
    ]
):
    """Install configured pi packages into the project's own `.pi/`.

    Project scope only. It writes `<project>/.pi/settings.json` (pi merges the
    `packages` key, keeps the other keys, and reformats the file),
    `<project>/.pi/npm/` (pi gitignores it itself), and for git sources
    `<project>/.pi/git/` (also self-gitignored). It writes nothing under
    `~/.pi/agent`. A symlinked `.pi` is followed, so installs land in its
    target.

    The FineCode `packages` config is authoritative for additions and pins.
    `.pi/settings.json`'s `packages` key is generated output. Entries the config
    no longer lists are never reconciled: they are logged and left in place
    (remove one with `pi remove <src> -l`).

    In a project that commits `.pi/settings.json`, register only shared packages
    in tracked config, so the generated `packages` key matches what is committed.
    Personal packages belong in `finecode-user.toml`, and only where
    `.pi/settings.json` is untracked.

    `--approve` extends FineCode's trust in the project to its
    `.pi/settings.json` for the `pi` commands this handler runs, including any
    project `npmCommand`, which pi executes during `pi install -l` and may
    execute during `pi list`. It loads no project extensions and persists
    nothing.

    The first install makes the project trust-requiring in pi. Interactive pi
    prompts once. RPC and print sessions, `PiAgentHandler` included, load none
    of the packages until the project is trusted.

    pi resolves project packages from the session's cwd only, so register this
    handler in the project where pi sessions start. A preset registration gives
    every including project its own `.pi/npm` and its own trust prompt. A
    subproject session does not see the root project's packages. Git sources
    clone into `<project>/.pi/git/`, which FineCode's project discovery does not
    skip, so a git package carrying a `pyproject.toml` would be discovered as a
    project too.

    List this handler after `install_pi`, which provides the `pi` binary.

    Configure one version per npm package: a second spec of a package already
    configured is reported `failed` rather than flip-flopping pi's same-identity
    replacement on every run.

    "Present" means a valid installed path under `Project packages:` in
    `pi list --approve`, plus a `package.json` version check for exact npm pins.
    Ranges and unpinned specs are checked for existence only. An install counts
    only once a follow-up `pi list` shows it with a path.

    Lifecycle scripts are suppressed for the installs this handler performs, and
    only while npm is the command (`npm_config_ignore_scripts`, the default;
    `allow_lifecycle_scripts = true` opts out). They still run on pi's startup
    self-heal in a trusted project (including inside `PiAgentHandler` sessions
    whose cwd is trusted), `pi update`, manual `pi install`, and under any custom
    `npmCommand` (global or project). `ignore-scripts=true` in `~/.npmrc` is the
    machine-wide control; this handler does not write it.

    Security: pi's docs warn that "Pi packages run with full system access".
    Pin npm versions and git refs. Pinned npm specs are also skipped by
    `pi update --extensions`.

    In a trusted project the packages load into every pi session with that cwd,
    `PiAgentHandler` runs included.
    """

    def __init__(
        self,
        config: InstallPiPackagesHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: SetupSystemRunPayload,
        run_context: SetupSystemRunContext,
    ) -> SetupSystemRunResult:
        packages = list(dict.fromkeys(self.config.packages))
        if not packages:
            return SetupSystemRunResult()

        if shutil.which("pi") is None:
            return SetupSystemRunResult(
                failed=[
                    f"pi package {src}: pi not found in PATH, install pi first "
                    f"(fine_agent_pi.InstallPiHandler)"
                    for src in packages
                ]
            )

        failed: list[str] = []
        supported: list[str] = []
        name_to_source: dict[str, str] = {}
        for src in packages:
            if not src.lower().startswith(_REMOTE_PREFIXES):
                failed.append(
                    f"pi package {src}: unsupported source; only npm: and git "
                    f"sources are supported (local paths are machine-specific; "
                    f"add them to pi settings directly)"
                )
                continue
            split = _split_npm_spec(src)
            if split is not None:
                name = split[0]
                first = name_to_source.get(name)
                if first is not None:
                    failed.append(
                        f"pi package {src}: same package as {first}; configure "
                        f"one version per package"
                    )
                    continue
                name_to_source[name] = src
            supported.append(src)

        if not supported:
            return SetupSystemRunResult(failed=failed)

        cwd = self.project_info_provider.get_current_project_dir_path()
        if self.config.allow_lifecycle_scripts:
            env = None
        else:
            env = {**os.environ, "npm_config_ignore_scripts": "true"}

        list_cmd = ["pi", "list", "--approve"]
        try:
            list_process = await self.command_runner.run(list_cmd, cwd=cwd, env=env)
        except (OSError, icommandrunner.CommandNotLaunchableError) as error:
            for src in supported:
                failed.append(
                    f"pi package {src}: "
                    f"{backend_support.spawn_error(list_cmd[0], error)}"
                )
            self.logger.error(backend_support.spawn_error(list_cmd[0], error))
            return SetupSystemRunResult(failed=failed)
        await list_process.wait_for_end()
        if list_process.get_exit_code() != 0:
            full, short = _error_text(list_process)
            for src in supported:
                failed.append(
                    f"pi package {src}: could not list installed pi packages: {short}"
                )
            self.logger.error(full)
            return SetupSystemRunResult(failed=failed)

        stderr = _ANSI_ESCAPE.sub("", list_process.get_error_output())
        warning_line = next(
            (line for line in stderr.splitlines() if _SETTINGS_WARNING.match(line)),
            None,
        )
        if warning_line is not None:
            for src in supported:
                failed.append(
                    f"pi package {src}: pi settings are unreadable, fix them "
                    f"first: {warning_line}"
                )
            self.logger.error(stderr)
            return SetupSystemRunResult(failed=failed)

        present = _parse_project_packages(list_process.get_output())
        skipped: list[str] = []
        to_install: list[str] = []
        for src in supported:
            path = present.get(src)
            pin = _exact_npm_pin(src)
            if path is not None and (
                pin is None or _installed_version_matches(path, pin[1])
            ):
                self.logger.info(f"pi package {src} already installed, skipping")
                skipped.append(f"pi package {src}")
                continue
            to_install.append(src)

        for src in present:
            if src not in packages:
                self.logger.info(
                    f"pi package {src} is in {cwd}/.pi/settings.json but not in "
                    f"the FineCode config; left in place (remove with: pi remove "
                    f"{src} -l)"
                )

        installed_sources: list[str] = []
        if to_install:
            async with run_context.progress(
                "Installing pi packages", total=len(to_install)
            ) as progress:
                for src in to_install:
                    await progress.report(f"pi install {src}")
                    install_cmd = ["pi", "install", src, "-l", "--approve"]
                    try:
                        process = await self.command_runner.run(
                            install_cmd, cwd=cwd, env=env
                        )
                    except (OSError, icommandrunner.CommandNotLaunchableError) as error:
                        failed.append(
                            f"pi package {src}: "
                            f"{backend_support.spawn_error(install_cmd[0], error)}"
                        )
                        self.logger.error(
                            backend_support.spawn_error(install_cmd[0], error)
                        )
                        continue
                    await process.wait_for_end()
                    await progress.advance(1)
                    if process.get_exit_code() == 0:
                        self.logger.info(f"pi package {src} installed")
                        installed_sources.append(src)
                    else:
                        full, short = _error_text(process)
                        failed.append(f"pi package {src}: {short}")
                        self.logger.error(full)

        if installed_sources:
            check_cmd = ["pi", "list", "--approve"]
            try:
                check_process = await self.command_runner.run(check_cmd, cwd=cwd, env=env)
            except (OSError, icommandrunner.CommandNotLaunchableError) as error:
                self.logger.warning(backend_support.spawn_error(check_cmd[0], error))
                return SetupSystemRunResult(failed=failed)
            await check_process.wait_for_end()
            if check_process.get_exit_code() != 0:
                _full, short = _error_text(check_process)
                self.logger.warning(short)
            else:
                after = _parse_project_packages(check_process.get_output())
                still_installed: list[str] = []
                for src in installed_sources:
                    if src not in after:
                        failed.append(
                            f"pi package {src}: pi reported success but did not "
                            f"record it in {cwd}/.pi/settings.json (is the file "
                            f"writable?)"
                        )
                    elif after[src] is None:
                        failed.append(
                            f"pi package {src}: pi recorded it but it is not "
                            f"installed on disk (check npmCommand in pi settings)"
                        )
                    else:
                        still_installed.append(src)
                installed_sources = still_installed

        return SetupSystemRunResult(
            installed=[f"pi package {src}" for src in installed_sources],
            skipped=skipped,
            failed=failed,
        )
