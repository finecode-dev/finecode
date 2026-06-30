from __future__ import annotations

import dataclasses
from typing import Any

from finecode_extension_api import code_action
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SemanticTokensPayload,
    SemanticTokensResult,
)
from fine_semantic_tokens.text_document_semantic_tokens_action import decode_lsp_semantic_tokens
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_python_lang.text_document_semantic_tokens_python_action import (
    TextDocumentSemanticTokensPythonAction,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class PyreflySemanticTokensHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflySemanticTokensHandler(
    code_action.ActionHandler[
        TextDocumentSemanticTokensPythonAction,
        PyreflySemanticTokensHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflySemanticTokensHandler")

    def __init__(
        self,
        config: PyreflySemanticTokensHandlerConfig,
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
        payload: SemanticTokensPayload,
        run_context: code_action.RunActionContext,
    ) -> SemanticTokensResult:
        file_path = resource_uri_to_path(payload.uri)

        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        async with self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session:
            async with session.read_file(file_path) as file_info:
                content = file_info.content

        range_dict: dict[str, Any] | None = None
        if payload.range is not None:
            range_dict = {
                "start": {
                    "line": payload.range.start.line,
                    "character": payload.range.start.character,
                },
                "end": {
                    "line": payload.range.end.line,
                    "character": payload.range.end.character,
                },
            }

        raw_result = await self.lsp_service.get_semantic_tokens(
            file_path, content, range_dict=range_dict
        )
        if not raw_result:
            return SemanticTokensResult(tokens=[])

        data: list[int] = raw_result.get("data") or []
        result_id: str | None = raw_result.get("resultId")

        caps = self.lsp_service.server_capabilities
        legend = caps.get("semanticTokensProvider", {}).get("legend", {})
        token_types: list[str] = legend.get("tokenTypes", [])
        token_modifiers: list[str] = legend.get("tokenModifiers", [])

        tokens = decode_lsp_semantic_tokens(data, token_types, token_modifiers)
        return SemanticTokensResult(tokens=tokens, result_id=result_id)
