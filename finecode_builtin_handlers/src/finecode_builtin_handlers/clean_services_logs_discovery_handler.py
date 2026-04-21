import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.clean_services_logs_action import (
    CleanServicesLogsAction,
    CleanServicesLogsRunContext,
    CleanServicesLogsRunPayload,
    CleanServicesLogsRunResult,
)
from finecode_extension_api.actions.observability.list_observability_services_action import (
    ListObservabilityServicesAction,
    ListObservabilityServicesRunPayload,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class CleanServicesLogsDiscoveryHandlerConfig(code_action.ActionHandlerConfig): ...


class CleanServicesLogsDiscoveryHandler(
    code_action.ActionHandler[
        CleanServicesLogsAction,
        CleanServicesLogsDiscoveryHandlerConfig,
    ]
):
    """Populate run_context.service_ids by calling list_observability_services.

    Must be registered before performing cleaning. No-op when
    run_context.service_ids is already set (i.e. explicit caller or repeated run).
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
        if run_context.service_ids is not None:
            return CleanServicesLogsRunResult()

        result = await self.project_action_runner.run_action(
            action_type=ListObservabilityServicesAction,
            payload=ListObservabilityServicesRunPayload(),
            meta=run_context.meta,
        )
        run_context.service_ids = [s.service_id for s in result.services]
        self.logger.debug(f"Discovered services to clean: {run_context.service_ids}")
        return CleanServicesLogsRunResult()
