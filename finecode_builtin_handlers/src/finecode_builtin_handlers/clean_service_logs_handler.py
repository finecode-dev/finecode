import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.clean_service_logs_action import (
    CleanServiceLogsAction,
    CleanServiceLogsRunContext,
    CleanServiceLogsRunPayload,
    CleanServiceLogsRunResult,
)
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
)

from finecode_builtin_handlers.observability_log_utils import resolve_log_dir


@dataclasses.dataclass
class CleanServiceLogsHandlerConfig(code_action.ActionHandlerConfig): ...


class CleanServiceLogsHandler(
    code_action.ActionHandler[
        CleanServiceLogsAction,
        CleanServiceLogsHandlerConfig,
    ]
):
    def __init__(
        self,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.runner_info_provider = runner_info_provider
        self.logger = logger

    async def run(
        self,
        payload: CleanServiceLogsRunPayload,
        run_context: CleanServiceLogsRunContext,
    ) -> CleanServiceLogsRunResult:
        log_dir = resolve_log_dir(payload.service_id, self.runner_info_provider)
        if not log_dir.is_dir():
            return CleanServiceLogsRunResult()

        errors: list[str] = []
        for log_file in log_dir.glob("*.log"):
            try:
                log_file.unlink()
                self.logger.info(f"Deleted {log_file}")
            except Exception as e:
                errors.append(str(e))

        return CleanServiceLogsRunResult(errors=errors)
