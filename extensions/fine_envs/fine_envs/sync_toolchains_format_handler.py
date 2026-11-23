# docs: docs/reference/actions.md
import dataclasses

from fine_format import format_file_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner

from fine_envs import sync_toolchains_action


@dataclasses.dataclass
class SyncToolchainsFormatHandlerConfig(code_action.ActionHandlerConfig): ...


class SyncToolchainsFormatHandler(
    code_action.ActionHandler[
        sync_toolchains_action.SyncToolchainsAction,
        SyncToolchainsFormatHandlerConfig,
    ]
):
    """Format the project definition file the toolchain sync just wrote.

    Only a file the sync actually wrote is formatted: a dry run (``save=False``, as
    ``check_toolchains`` uses) or an up-to-date axis leaves the tree untouched.

    No formatter covering the file is not an error.
    The miss stays visible as coverage. A formatter that fails, or a formatted
    file that could not be saved because it changed meanwhile, does fail the run,
    after the axis is already written; disable this handler to keep the writer's
    layout.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: sync_toolchains_action.SyncToolchainsRunPayload,
        run_context: sync_toolchains_action.SyncToolchainsRunContext,
    ) -> sync_toolchains_action.SyncToolchainsRunResult:
        sync_result = run_context.current_result
        project_def_file = (
            sync_result.project_def_file
            if isinstance(sync_result, sync_toolchains_action.SyncToolchainsRunResult)
            else None
        )
        if (
            project_def_file is None
            or project_def_file.status
            is not sync_toolchains_action.ProjectDefFileStatus.UPDATED
        ):
            return sync_toolchains_action.SyncToolchainsRunResult()
        saved_file_path = project_def_file.path

        coverage: list[ItemCoverage] = []
        try:
            result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    format_file_action.FormatFileAction
                ),
                payload=format_file_action.FormatFileRunPayload(
                    file_path=saved_file_path, save=True
                ),
                meta=run_context.meta,
            )
        except iprojectactionrunner.ActionNotFound:
            # No dispatcher ran to record the miss, so record it here: the file
            # has no formatter, the same answer as "no subactions registered".
            coverage = [
                ItemCoverage(
                    status=CoverageStatus.NO_SUBACTIONS,
                    item=saved_file_path,
                    detail="format_file",
                )
            ]
            self.logger.warning(
                "sync_toolchains: no format_file action registered; the synced file"
                " is left unformatted"
            )
        except iprojectactionrunner.ActionRunFailed as exc:
            raise code_action.ActionFailedException(
                f"The toolchain axis was written to {saved_file_path}, but formatting"
                " it failed (disable handler 'sync_toolchains_format' to keep the"
                " unformatted file):\n  - " + exc.message
            ) from exc
        else:
            if result.status is format_file_action.FormatFileStatus.SAVE_REFUSED:
                raise code_action.ActionFailedException(
                    f"The toolchain axis was written to {saved_file_path}, but the"
                    " formatted file was not saved: the file changed after the"
                    " formatter read it. Run sync_toolchains again."
                )
            if result.unhandled:
                # Not absorbed: the miss reaches this run's result through the
                # coverage sink.
                self.logger.warning(
                    "sync_toolchains: no formatter covers the synced file; it is left"
                    " unformatted"
                )

        return sync_toolchains_action.SyncToolchainsRunResult(coverage=coverage)
