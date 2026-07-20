import dataclasses
import json

from packaging.version import InvalidVersion, Version

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, ilogger
from fine_envs.list_obtainable_toolchains_action import (
    ListObtainableToolchainsRunResult,
)
from fine_python_lang import list_obtainable_python_interpreters_action

from ._uv_common import get_uv_executable


@dataclasses.dataclass
class UvListObtainablePythonInterpretersHandlerConfig(code_action.ActionHandlerConfig):
    variant: str = "default"
    """Build variant to report. ``freethreaded`` builds are a separate variant that the
    ``(implementation, version)`` identity model cannot express, so they are excluded."""


class UvListObtainablePythonInterpretersHandler(
    code_action.ActionHandler[
        list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersAction,
        UvListObtainablePythonInterpretersHandlerConfig,
    ]
):
    """Report the interpreters uv can download, reduced to matrix-axis granularity.

    uv is the authority here because it is what provisions interpreters for
    ``create_env``: a version uv cannot obtain would produce an env that cannot be
    built. ``--only-downloads`` is deliberate — it reports uv's own manifest rather
    than what is installed on this machine, so the answer is a function of the locked
    uv version and not of local state.

    uv's raw listing is much finer-grained than a matrix axis: it carries patch levels,
    prereleases, freethreaded variants and platform tags. All of that is collapsed away,
    leaving one identity per implementation and minor version.
    """

    def __init__(
        self,
        config: UvListObtainablePythonInterpretersHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger

    async def run(
        self,
        payload: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunPayload,
        run_context: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunContext,
    ) -> ListObtainableToolchainsRunResult:
        uv_executable = get_uv_executable()
        command = (
            f"{uv_executable} python list --only-downloads --all-versions"
            " --output-format json"
        )
        process = await self.command_runner.run(cmd=command)
        await process.wait_for_end()

        if process.get_exit_code() != 0:
            raise code_action.ActionFailedException(
                f"Failed to list obtainable Python interpreters: {process.get_error_output()}"
            )

        try:
            entries = json.loads(process.get_output())
        except json.JSONDecodeError as error:
            raise code_action.ActionFailedException(
                f"Could not parse `uv python list` output: {error}"
            ) from error

        toolchains: set[tuple[str, Version]] = set()
        considered = 0
        unparsable = 0
        for entry in entries:
            if entry.get("variant") != self.config.variant:
                continue
            considered += 1

            # every field the entry is read for is parsed under one guard: a uv release
            # that renames or drops any of them must degrade to a skipped row, not take
            # down every sync_toolchains run with a raw KeyError
            try:
                version = Version(entry["version"])
                implementation = entry["implementation"]
                parts = entry["version_parts"]
                minor_series = Version(f"{parts['major']}.{parts['minor']}")
            except (InvalidVersion, KeyError, TypeError):
                unparsable += 1
                self.logger.debug(f"Skipping unparsable uv entry: {entry.get('key')}")
                continue

            if version.is_prerelease and not payload.include_prereleases:
                continue

            toolchains.add((implementation, minor_series))

        # Skipping an individual malformed row is tolerance; failing to read *every* row
        # is schema drift, and must not degrade quietly -- an empty axis would surface
        # far downstream as "requires-python matches no obtainable CPython version".
        # Keyed on parse failures, not on an empty result: legitimately empty is normal
        # (a variant with no builds, or every row filtered out as a prerelease).
        if considered and unparsable == considered:
            raise code_action.ActionFailedException(
                f"Could not read any of the {considered} entries `uv python list`"
                " returned -- uv's output format has likely changed"
            )

        return ListObtainableToolchainsRunResult(
            toolchains=[
                f"{implementation}@{version}"
                for implementation, version in sorted(toolchains)
            ]
        )
