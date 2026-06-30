from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_symbol_info.text_document_definition_action import DefinitionPayload, DefinitionResult
from fine_python_lang.text_document_definition_python_action import TextDocumentDefinitionPythonAction
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly._lsp_location_utils import locations_from_lsp


@dataclasses.dataclass
class PyreflyDefinitionHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyDefinitionHandler(
    code_action.ActionHandler[
        TextDocumentDefinitionPythonAction,
        PyreflyDefinitionHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflyDefinitionHandler")

    def __init__(
        self,
        config: PyreflyDefinitionHandlerConfig,
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
        payload: DefinitionPayload,
        run_context: code_action.RunActionContext,
    ) -> DefinitionResult:
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

        result = await self.lsp_service.get_definition(file_path, content, position)
        return DefinitionResult(locations=locations_from_lsp(result))
