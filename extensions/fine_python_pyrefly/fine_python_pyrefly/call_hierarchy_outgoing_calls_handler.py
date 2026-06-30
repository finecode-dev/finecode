from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_code_hierarchy.call_hierarchy_outgoing_calls_action import (
    CallHierarchyOutgoingCallsPayload,
    CallHierarchyOutgoingCallsResult,
)
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_python_lang.call_hierarchy_outgoing_calls_python_action import (
    CallHierarchyOutgoingCallsPythonAction,
)
from fine_python_pyrefly._lsp_hierarchy_utils import (
    call_hierarchy_item_to_lsp,
    outgoing_call_from_lsp,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class PyreflyCallHierarchyOutgoingCallsHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyCallHierarchyOutgoingCallsHandler(
    code_action.ActionHandler[
        CallHierarchyOutgoingCallsPythonAction,
        PyreflyCallHierarchyOutgoingCallsHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="PyreflyCallHierarchyOutgoingCallsHandler"
    )

    def __init__(
        self,
        config: PyreflyCallHierarchyOutgoingCallsHandlerConfig,
        file_editor: ifileeditor.IFileEditor,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: PyreflyLspService,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.file_editor = file_editor
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service
        self.logger = logger

    async def run(
        self,
        payload: CallHierarchyOutgoingCallsPayload,
        run_context: code_action.RunActionContext,
    ) -> CallHierarchyOutgoingCallsResult:
        file_path = resource_uri_to_path(payload.item.uri)

        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        async with self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session:
            async with session.read_file(file_path) as file_info:
                content = file_info.content

        item_dict = call_hierarchy_item_to_lsp(payload.item)
        raw_result = await self.lsp_service.get_call_hierarchy_outgoing_calls(
            file_path, content, item_dict
        )
        if not raw_result:
            return CallHierarchyOutgoingCallsResult(calls=[])

        return CallHierarchyOutgoingCallsResult(
            calls=[outgoing_call_from_lsp(d) for d in raw_result]
        )
