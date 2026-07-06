# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from fine_format import check_formatting_action, format_action


@dataclasses.dataclass
class CheckFormattingHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckFormattingHandler(
    code_action.ActionHandler[
        check_formatting_action.CheckFormattingAction, CheckFormattingHandlerConfig
    ]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self.action_runner = action_runner

    async def run(
        self,
        payload: check_formatting_action.CheckFormattingRunPayload,
        run_context: check_formatting_action.CheckFormattingRunContext,
    ) -> check_formatting_action.CheckFormattingRunResult:
        format_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                format_action.FormatAction
            ),
            payload=format_action.FormatRunPayload(
                target=payload.target,
                file_paths=payload.file_paths,
                save=False,
            ),
            meta=run_context.meta,
        )
        files_needing_format = [
            file_uri
            for file_uri, file_result in format_result.result_by_file_path.items()
            if file_result.changed
        ]
        return check_formatting_action.CheckFormattingRunResult(
            files_needing_format=files_needing_format
        )
