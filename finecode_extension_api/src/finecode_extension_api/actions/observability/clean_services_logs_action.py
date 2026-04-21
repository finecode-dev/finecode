# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class CleanServicesLogsRunPayload(code_action.RunActionPayload):
    service_ids: list[str] | None = None
    """Services whose logs should be deleted.
    None = discover and clean all available services (requires a discovery handler).
    Empty list = explicit no-op."""


class CleanServicesLogsRunContext(
    code_action.RunActionContext[CleanServicesLogsRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: CleanServicesLogsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            progress_sender=progress_sender,
        )
        self.service_ids: list[str] | None = None
        """None = discovery has not run. [] = explicit no-op."""

    async def init(self) -> None:
        if self.initial_payload.service_ids is not None:
            self.service_ids = list(self.initial_payload.service_ids)


@dataclasses.dataclass
class CleanServicesLogsRunResult(code_action.RunActionResult):
    services_cleaned: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CleanServicesLogsRunResult):
            return
        self.services_cleaned += other.services_cleaned
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        if self.errors:
            return "\n".join(self.errors)
        return f"Cleaned {len(self.services_cleaned)} service(s)."

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return (
            code_action.RunReturnCode.ERROR
            if self.errors
            else code_action.RunReturnCode.SUCCESS
        )


class CleanServicesLogsAction(
    code_action.Action[
        CleanServicesLogsRunPayload,
        CleanServicesLogsRunContext,
        CleanServicesLogsRunResult,
    ]
):
    """Delete logs for multiple observability services.

    When service_ids is None, a discovery handler must run first to populate
    run_context.service_ids from list_observability_services. No-op when
    service_ids is an empty list.
    """

    PAYLOAD_TYPE = CleanServicesLogsRunPayload
    RUN_CONTEXT_TYPE = CleanServicesLogsRunContext
    RESULT_TYPE = CleanServicesLogsRunResult
