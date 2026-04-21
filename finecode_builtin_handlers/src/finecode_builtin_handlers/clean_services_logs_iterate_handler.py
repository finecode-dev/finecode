import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.clean_service_logs_action import (
    CleanServiceLogsAction,
    CleanServiceLogsRunPayload,
)
from finecode_extension_api.actions.observability.clean_services_logs_action import (
    CleanServicesLogsAction,
    CleanServicesLogsRunContext,
    CleanServicesLogsRunPayload,
    CleanServicesLogsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class CleanServicesLogsIterateHandlerConfig(code_action.ActionHandlerConfig): ...


class CleanServicesLogsIterateHandler(
    code_action.ActionHandler[
        CleanServicesLogsAction,
        CleanServicesLogsIterateHandlerConfig,
    ]
):
    """Call clean_service_logs for each service_id in run_context.service_ids.

    Must be registered after discovery handler.
    """

    def __init__(
        self,
        project_action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_action_runner = project_action_runner
        self.logger = logger

    async def run(
        self,
        payload: CleanServicesLogsRunPayload,
        run_context: CleanServicesLogsRunContext,
    ) -> CleanServicesLogsRunResult:
        if run_context.service_ids is None:
            raise code_action.ActionFailedException(
                "CleanServicesLogsDiscoveryHandler must be registered before"
                " CleanServicesLogsIterateHandler"
            )
        if not run_context.service_ids:
            return CleanServicesLogsRunResult()

        services_cleaned: list[str] = []
        errors: list[str] = []

        for service_id in run_context.service_ids:
            result = await self.project_action_runner.run_action(
                action_type=CleanServiceLogsAction,
                payload=CleanServiceLogsRunPayload(service_id=service_id),
                meta=run_context.meta,
            )
            if result.errors:
                errors.extend(result.errors)
            else:
                services_cleaned.append(service_id)

        return CleanServicesLogsRunResult(
            services_cleaned=services_cleaned,
            errors=errors,
        )
