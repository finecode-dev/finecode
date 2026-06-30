from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.common_types import Position, Range
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_symbol_info.text_document_hover_action import HoverPayload, HoverResult, MarkupContent, MarkupKind
from fine_python_lang.text_document_hover_python_action import TextDocumentHoverPythonAction
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


def _position_from_lsp(p: dict) -> Position:
    return Position(line=p["line"], character=p["character"])


def _range_from_lsp(r: dict) -> Range:
    return Range(
        start=_position_from_lsp(r["start"]),
        end=_position_from_lsp(r["end"]),
    )


@dataclasses.dataclass
class PyreflyHoverHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyHoverHandler(
    code_action.ActionHandler[
        TextDocumentHoverPythonAction,
        PyreflyHoverHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflyHoverHandler")

    def __init__(
        self,
        config: PyreflyHoverHandlerConfig,
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
        payload: HoverPayload,
        run_context: code_action.RunActionContext,
    ) -> HoverResult:
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

        raw = await self.lsp_service.get_hover(file_path, content, position)
        if not raw:
            return HoverResult()

        contents = raw.get("contents") or {}
        if isinstance(contents, str):
            mc = MarkupContent(kind=MarkupKind.PLAINTEXT, value=contents)
        elif isinstance(contents, dict):
            mc = MarkupContent(
                kind=MarkupKind(contents.get("kind", "plaintext")),
                value=contents.get("value", ""),
            )
        else:
            return HoverResult()

        lsp_range = raw.get("range")
        fc_range = _range_from_lsp(lsp_range) if lsp_range else None
        return HoverResult(content=mc, range=fc_range)
