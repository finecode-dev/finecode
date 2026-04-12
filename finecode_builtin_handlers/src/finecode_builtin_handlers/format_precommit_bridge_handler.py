import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_action, precommit_action
from finecode_extension_api.interfaces import iprojectactionrunner, ilogger
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri


@dataclasses.dataclass
class _FormatCheckResult(code_action.RunActionResult):
    files_needing_format: list[ResourceUri] = dataclasses.field(default_factory=list)

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        if self.files_needing_format:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


@dataclasses.dataclass
class FormatPrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatPrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, FormatPrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that checks formatting of staged files without modifying them."""

    def __init__(
        self,
        project_action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_action_runner = project_action_runner
        self.logger = logger

    async def run(
        self,
        payload: precommit_action.PrecommitRunPayload,
        run_context: precommit_action.PrecommitRunContext,
    ) -> precommit_action.PrecommitRunResult:
        if run_context.staged_files is None:
            raise code_action.ActionFailedException(
                "discovery handler must be registered before bridge handlers"
            )
        if not run_context.staged_files:
            self.logger.info("No staged files - skipping format check.")
            return precommit_action.PrecommitRunResult()

        file_uris = [path_to_resource_uri(p) for p in run_context.staged_files]
        result = await self.project_action_runner.run_action(
            action_type=format_action.FormatAction,
            payload=format_action.FormatRunPayload(
                target=format_action.FormatTarget.FILES,
                file_paths=file_uris,
                save=False,
            ),
            meta=run_context.meta,
        )
        files_needing_format = [
            file_uri
            for file_uri, file_result in result.result_by_file_path.items()
            if file_result.changed
        ]
        if files_needing_format:
            self.logger.info(
                f"{len(files_needing_format)} file(s) need formatting: "
                + ", ".join(str(f) for f in files_needing_format)
            )
        check_result = _FormatCheckResult(files_needing_format=files_needing_format)
        return precommit_action.PrecommitRunResult(
            action_results={"format": check_result}
        )
