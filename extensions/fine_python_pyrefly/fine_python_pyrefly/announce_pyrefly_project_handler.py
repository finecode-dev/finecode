from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger

from fine_python_pyrefly.announce_pyrefly_project_action import (
    AnnouncePyreflyProjectAction,
)
from fine_python_pyrefly.host_types import (
    AnnouncePyreflyProjectPayload,
    AnnouncePyreflyProjectResult,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class AnnouncePyreflyProjectHandlerConfig(code_action.ActionHandlerConfig):
    pass


class AnnouncePyreflyProjectHandler(
    code_action.ActionHandler[
        AnnouncePyreflyProjectAction, AnnouncePyreflyProjectHandlerConfig
    ]
):
    """Register a forwarded project with the workspace host.

    The refusal (version/topology mismatch) comes back as ``registered=False``
    with the configuration-error text rather than an exception: the project's
    own request then fails with it, while already-registered projects keep
    working.
    """

    def __init__(
        self,
        config: AnnouncePyreflyProjectHandlerConfig,
        pyrefly_lsp_service: PyreflyLspService,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.pyrefly_lsp_service = pyrefly_lsp_service
        self.logger = logger

    async def run(
        self,
        payload: AnnouncePyreflyProjectPayload,
        run_context: code_action.RunActionContext[AnnouncePyreflyProjectPayload],
    ) -> AnnouncePyreflyProjectResult:
        self.logger.debug(
            f"Run {run_context.run_id}: registering pyrefly project"
            f" {payload.identity.project_path}"
        )
        try:
            self.pyrefly_lsp_service.register_project(payload.identity)
        except code_action.ActionFailedException as exc:
            self.logger.warning(
                f"Refused pyrefly project {payload.identity.project_path}:"
                f" {exc.message}"
            )
            return AnnouncePyreflyProjectResult(registered=False, error=exc.message)
        return AnnouncePyreflyProjectResult(registered=True)
