from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    PrepareTypeHierarchyPayload,
    PrepareTypeHierarchyResult,
)
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_python_lang.text_document_prepare_type_hierarchy_python_action import (
    TextDocumentPrepareTypeHierarchyPythonAction,
)
from fine_python_pyrefly._lsp_hierarchy_utils import type_hierarchy_item_from_lsp
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class PyreflyPrepareTypeHierarchyHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyPrepareTypeHierarchyHandler(
    code_action.ActionHandler[
        TextDocumentPrepareTypeHierarchyPythonAction,
        PyreflyPrepareTypeHierarchyHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="PyreflyPrepareTypeHierarchyHandler"
    )

    def __init__(
        self,
        config: PyreflyPrepareTypeHierarchyHandlerConfig,
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
        payload: PrepareTypeHierarchyPayload,
        run_context: code_action.RunActionContext,
    ) -> PrepareTypeHierarchyResult:
        file_path = resource_uri_to_path(payload.uri)

        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        async with self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session:
            async with session.read_file(file_path) as file_info:
                content = file_info.content

        position = {
            "line": payload.position.line,
            "character": payload.position.character,
        }

        raw_result = await self.lsp_service.get_type_hierarchy_prepare(
            file_path, content, position
        )
        if not raw_result:
            return PrepareTypeHierarchyResult(items=[])

        return PrepareTypeHierarchyResult(
            items=[type_hierarchy_item_from_lsp(d) for d in raw_result]
        )
