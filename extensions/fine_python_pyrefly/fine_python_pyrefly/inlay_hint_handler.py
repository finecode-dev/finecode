from __future__ import annotations

import dataclasses
from typing import Any

from finecode_extension_api import code_action
from finecode_extension_api.common_types import Position
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_inlay_hints.text_document_inlay_hint import (
    InlayHint,
    InlayHintKind,
    InlayHintPayload,
    InlayHintResult,
)
from fine_python_lang.text_document_inlay_hint_python_action import (
    TextDocumentInlayHintPythonAction,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


def _inlay_hint_from_lsp(d: dict[str, Any]) -> InlayHint:
    label = d.get("label", "")
    if isinstance(label, list):
        label = "".join(part.get("value", "") for part in label)

    position = d.get("position") or {}
    return InlayHint(
        position=Position(
            line=position.get("line", 0), character=position.get("character", 0)
        ),
        label=label,
        kind=InlayHintKind(d.get("kind", InlayHintKind.TYPE)),
        padding_left=d.get("paddingLeft", False),
        padding_right=d.get("paddingRight", False),
    )


@dataclasses.dataclass
class PyreflyInlayHintHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyInlayHintHandler(
    code_action.ActionHandler[
        TextDocumentInlayHintPythonAction,
        PyreflyInlayHintHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="PyreflyInlayHintHandler")

    def __init__(
        self,
        config: PyreflyInlayHintHandlerConfig,
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
        payload: InlayHintPayload,
        run_context: code_action.RunActionContext,
    ) -> InlayHintResult:
        file_path = resource_uri_to_path(payload.uri)

        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        async with self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session:
            async with session.read_file(file_path) as file_info:
                content = file_info.content

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

        raw_result = await self.lsp_service.get_inlay_hints(file_path, content, range_dict)
        if not raw_result:
            return InlayHintResult(hints=[])

        return InlayHintResult(hints=[_inlay_hint_from_lsp(d) for d in raw_result])
