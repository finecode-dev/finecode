from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_symbol_info.text_document_references_action import ReferencesPayload, ReferencesResult
from fine_python_lang.text_document_references_python_action import TextDocumentReferencesPythonAction
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly._lsp_location_utils import locations_from_lsp


@dataclasses.dataclass
class PyreflyReferencesHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyReferencesHandler(
    code_action.ActionHandler[
        TextDocumentReferencesPythonAction,
        PyreflyReferencesHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflyReferencesHandler")

    def __init__(
        self,
        config: PyreflyReferencesHandlerConfig,
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
        payload: ReferencesPayload,
        run_context: code_action.RunActionContext,
    ) -> ReferencesResult:
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

        result = await self.lsp_service.get_references(file_path, content, position, payload.include_declaration)
        return ReferencesResult(locations=locations_from_lsp(result))
