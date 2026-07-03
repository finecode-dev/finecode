import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_inspect_code.inspect_code_action import (
    InspectCodeAction,
    InspectCodeRunPayload,
    InspectCodeRunContext,
    InspectCodeRunResult,
    InspectCodeTarget,
)
from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)


@dataclasses.dataclass
class FileExistenceValidationHandlerConfig(code_action.ActionHandlerConfig): ...


class FileExistenceValidationHandler(
    code_action.ActionHandler[InspectCodeAction, FileExistenceValidationHandlerConfig]
):
    """Enforce the inspect_code contract for missing files.

    When ``target="files"``, each path in ``file_paths`` must exist on disk.
    Bridge handlers (lint, type_check, …) silently produce empty results for
    missing files, which would otherwise appear as "OK" in the output.  This
    handler emits a WARNING diagnostic for each path that does not exist so
    callers can distinguish "checked and clean" from "file not found".
    """

    def __init__(self, logger: ilogger.ILogger) -> None:
        self.logger = logger

    async def run(
        self,
        payload: InspectCodeRunPayload,
        run_context: InspectCodeRunContext,
    ) -> None:
        if payload.target != InspectCodeTarget.FILES or not payload.file_paths:
            return

        for uri in payload.file_paths:
            path = pathlib.Path(resource_uri_to_path(uri))
            if not path.exists():
                self.logger.warning(
                    f"FileExistenceValidationHandler: file does not exist: {uri}"
                )
                await run_context.partial_result_sender.send(
                    InspectCodeRunResult(
                        messages={
                            uri: [
                                Diagnostic(
                                    range=Range(
                                        start=Position(line=0, character=0),
                                        end=Position(line=0, character=0),
                                    ),
                                    message="File does not exist",
                                    source="finecode",
                                    severity=DiagnosticSeverity.WARNING,
                                )
                            ]
                        }
                    )
                )
