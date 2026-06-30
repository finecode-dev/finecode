from __future__ import annotations

import dataclasses
from typing import Any

from finecode_extension_api import code_action, common_types
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_symbol_info.text_document_document_highlight_action import (
    DocumentHighlight,
    DocumentHighlightKind,
    DocumentHighlightPayload,
    DocumentHighlightResult,
)
from fine_python_lang.text_document_document_highlight_python_action import (
    TextDocumentDocumentHighlightPythonAction,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


def _range_from_lsp(d: dict[str, Any]) -> common_types.Range:
    start = d.get("start", {})
    end = d.get("end", {})
    return common_types.Range(
        start=common_types.Position(
            line=start.get("line", 0),
            character=start.get("character", 0),
        ),
        end=common_types.Position(
            line=end.get("line", 0),
            character=end.get("character", 0),
        ),
    )


@dataclasses.dataclass
class PyreflyDocumentHighlightHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyDocumentHighlightHandler(
    code_action.ActionHandler[
        TextDocumentDocumentHighlightPythonAction,
        PyreflyDocumentHighlightHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflyDocumentHighlightHandler")

    def __init__(
        self,
        config: PyreflyDocumentHighlightHandlerConfig,
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
        payload: DocumentHighlightPayload,
        run_context: code_action.RunActionContext,
    ) -> DocumentHighlightResult:
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

        result = await self.lsp_service.get_document_highlight(file_path, content, position)
        if not result:
            return DocumentHighlightResult()
        highlights = [
            DocumentHighlight(
                range=_range_from_lsp(item["range"]),
                kind=DocumentHighlightKind(
                    item.get("kind", DocumentHighlightKind.TEXT.value)
                ),
            )
            for item in result
        ]
        return DocumentHighlightResult(highlights=highlights)
