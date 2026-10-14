from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.contrib.lsp_service import LspDiagnosticsUnavailable
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_python_pyrefly.check_files_on_pyrefly_host_action import (
    CheckFilesOnPyreflyHostAction,
)
from fine_python_pyrefly.host_types import (
    CheckFilesOnPyreflyHostPayload,
    CheckFilesOnPyreflyHostResult,
    FileCheckOutcome,
    FileCheckStatus,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class CheckFilesOnPyreflyHostHandlerConfig(code_action.ActionHandlerConfig):
    pass


class CheckFilesOnPyreflyHostHandler(
    code_action.ActionHandler[
        CheckFilesOnPyreflyHostAction, CheckFilesOnPyreflyHostHandlerConfig
    ]
):
    """Check a forwarded chunk of files on the workspace host's server.

    Every file produces an outcome: a clean or failing check is data the caller
    keeps a per-file channel for, so one unavailable file never cancels its
    siblings (ADR-0109).
    """

    def __init__(
        self,
        config: CheckFilesOnPyreflyHostHandlerConfig,
        pyrefly_lsp_service: PyreflyLspService,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.pyrefly_lsp_service = pyrefly_lsp_service
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: CheckFilesOnPyreflyHostPayload,
        run_context: code_action.RunActionContext[CheckFilesOnPyreflyHostPayload],
    ) -> CheckFilesOnPyreflyHostResult:
        self.logger.debug(
            f"Run {run_context.run_id}: checking {len(payload.files)} files for"
            f" {payload.identity.project_path}"
        )
        self.pyrefly_lsp_service.register_project(payload.identity)
        root_uri = (
            self.project_info_provider.get_current_project_dir_path().as_uri()
        )
        await self.pyrefly_lsp_service.ensure_started(root_uri)
        sweep_paths = (
            [resource_uri_to_path(uri) for uri in payload.files]
            if payload.sweep
            else []
        )
        await self.pyrefly_lsp_service.ensure_forwarded(
            payload.identity, sweep_paths=sweep_paths
        )

        outcomes: list[FileCheckOutcome] = []
        for uri in payload.files:
            file_path = resource_uri_to_path(uri)
            try:
                diagnostics = await self.pyrefly_lsp_service.check_file(
                    file_path, content=payload.contents.get(uri)
                )
            except LspDiagnosticsUnavailable as exc:
                outcomes.append(
                    FileCheckOutcome(
                        uri=uri,
                        status=FileCheckStatus.FAILED,
                        diagnostics=[],
                        error=str(exc),
                    )
                )
            else:
                outcomes.append(
                    FileCheckOutcome(
                        uri=uri,
                        status=FileCheckStatus.CHECKED,
                        diagnostics=diagnostics,
                    )
                )
        return CheckFilesOnPyreflyHostResult(files=outcomes)
