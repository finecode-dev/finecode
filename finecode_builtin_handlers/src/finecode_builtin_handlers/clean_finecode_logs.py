import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    clean_finecode_logs as clean_finecode_logs_action,
)
from finecode_extension_api.interfaces import ilogger, iextensionrunnerinfoprovider


@dataclasses.dataclass
class CleanFinecodeLogsHandlerConfig(code_action.ActionHandlerConfig): ...


class CleanFinecodeLogsHandler(
    code_action.ActionHandler[
        clean_finecode_logs_action.CleanFinecodeLogsAction,
        CleanFinecodeLogsHandlerConfig,
    ]
):
    def __init__(
        self,
        logger: ilogger.ILogger,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
    ) -> None:
        self.logger = logger
        self.extension_runner_info_provider = extension_runner_info_provider

    async def run(
        self,
        payload: clean_finecode_logs_action.CleanFinecodeLogsRunPayload,
        run_context: clean_finecode_logs_action.CleanFinecodeLogsRunContext,
    ) -> clean_finecode_logs_action.CleanFinecodeLogsRunResult:
        venv_dir_path = self.extension_runner_info_provider.get_current_venv_dir_path()
        logs_dir_path = venv_dir_path / "logs"
        errors: list[str] = []

        # use file manager instead?
        for log_file_path in logs_dir_path.glob("*.log"):
            try:
                log_file_path.unlink()
            except Exception as exception:
                errors += str(exception)

            self.logger.info(f"Deleted {log_file_path}")

        return clean_finecode_logs_action.CleanFinecodeLogsRunResult(errors=errors)
