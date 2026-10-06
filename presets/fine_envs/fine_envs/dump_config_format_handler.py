# docs: docs/reference/actions.md
import dataclasses

from fine_format import format_file_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner

from fine_envs import dump_config_action


@dataclasses.dataclass
class DumpConfigFormatHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigFormatHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigFormatHandlerConfig
    ]
):
    """Format the rendered dump with the project's formatter for the target file.

    The content is handed to ``format_file`` in memory (``save=False``) and the
    result replaces ``config_dump_content``, so the save handler writes once.
    Which formatter runs is decided by ``format_file``'s dispatch on the target
    file, never here.

    No formatter covering the target file is not an error: this handler is
    registered for every project by the mandatory ``fine_envs`` preset, and a
    project without a formatter for the file would not flag the unformatted
    dump in ``check_formatting`` either. The miss stays visible as coverage and
    is never absorbed, because the caller asked for a formatted dump. A
    formatter that fails does fail the dump; disable this handler to write the
    dump unformatted. A caller that wants a raw dump (machine input, or the
    formatter unavailable) passes ``format_output: False`` on the payload: no
    ``format_file`` dispatch happens, the rendered content is saved unchanged
    and the result carries no coverage.
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
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        if run_context.config_dump_content is None:
            raise code_action.ActionFailedException(
                "dump_config_format: no rendered dump content; the dump_config"
                " handler must run before this one"
            )
        if not payload.format_output:
            # The caller asked for no formatting: nothing was dispatched, so
            # there is nothing to miss, and no coverage entry to record (R-310
            # guards inputs no subaction covered; the caller opted out of
            # subactions entirely).
            self.logger.debug("dump_config: formatting disabled by the caller")
            return dump_config_action.DumpConfigRunResult(
                config_dump=run_context.raw_config_dump
            )
        coverage: list[ItemCoverage] = []
        try:
            result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    format_file_action.FormatFileAction
                ),
                payload=format_file_action.FormatFileRunPayload(
                    file_path=payload.target_file_path, save=False
                ),
                meta=run_context.meta,
                caller_kwargs=format_file_action.FormatFileCallerRunContextKwargs(
                    file_editor_session=None,  # non-serializable; must stay None
                    file_info=format_file_action.FileInfo(
                        file_content=run_context.config_dump_content,
                        file_version="",
                    ),
                ),
            )
        except iprojectactionrunner.ActionNotFound:
            # No dispatcher ran to record the miss, so record it here: the dump
            # has no formatter, the same answer as "no subactions registered".
            coverage = [
                ItemCoverage(
                    status=CoverageStatus.NO_SUBACTIONS,
                    item=payload.target_file_path,
                    detail="format_file",
                )
            ]
            self.logger.warning(
                "dump_config: no format_file action registered; writing unformatted dump"
            )
        except iprojectactionrunner.ActionRunFailed as exc:
            raise code_action.ActionFailedException(
                "Formatting the config dump failed (pass format_output=false, or"
                " disable handler 'dump_config_format', to write it unformatted):\n  - "
                + exc.message
            ) from exc
        else:
            if result.unhandled:
                # Not absorbed: the miss reaches this run's result through the
                # coverage sink, which is how the caller learns the dump is
                # unformatted.
                self.logger.warning(
                    "dump_config: no formatter covers the dump file; writing unformatted dump"
                )
            elif result.changed:
                run_context.config_dump_content = result.code

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump, coverage=coverage
        )
