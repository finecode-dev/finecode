# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class ServiceInfo:
    service_id: str
    """Stable identifier."""
    description: str = ""


@dataclasses.dataclass
class ListObservabilityServicesRunPayload(code_action.RunActionPayload): ...


class ListObservabilityServicesRunContext(
    code_action.RunActionContext[ListObservabilityServicesRunPayload]
): ...


@dataclasses.dataclass
class ListObservabilityServicesRunResult(code_action.RunActionResult):
    services: list[ServiceInfo] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ListObservabilityServicesRunResult):
            return
        self.services += other.services

    def to_text(self) -> str | textstyler.StyledText:
        if not self.services:
            return "No services found."
        return "\n".join(
            f"{s.service_id}" + (f" — {s.description}" if s.description else "")
            for s in self.services
        )


class ListObservabilityServicesAction(
    code_action.Action[
        ListObservabilityServicesRunPayload,
        ListObservabilityServicesRunContext,
        ListObservabilityServicesRunResult,
    ]
):
    """Discover available observability services."""

    PAYLOAD_TYPE = ListObservabilityServicesRunPayload
    RUN_CONTEXT_TYPE = ListObservabilityServicesRunContext
    RESULT_TYPE = ListObservabilityServicesRunResult
