from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ifileeditor,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_pyrefly.host_types import (
    PyreflyItemQuery,
    PyreflyPositionQuery,
    PyreflyQueryKind,
    PyreflyRangeQuery,
    QueryPyreflyHostPayload,
    QueryPyreflyHostResult,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly.query_pyrefly_host_action import QueryPyreflyHostAction


@dataclasses.dataclass
class QueryPyreflyHostHandlerConfig(code_action.ActionHandlerConfig):
    pass


class QueryPyreflyHostHandler(
    code_action.ActionHandler[QueryPyreflyHostAction, QueryPyreflyHostHandlerConfig]
):
    """Answer one forwarded IDE query on the workspace host's server.

    The raw LSP result is passed through: the caller's handler does the
    FineCode conversion, so the host adds no second interpretation.
    """

    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="QueryPyreflyHostHandler")

    def __init__(
        self,
        config: QueryPyreflyHostHandlerConfig,
        pyrefly_lsp_service: PyreflyLspService,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.pyrefly_lsp_service = pyrefly_lsp_service
        self.project_info_provider = project_info_provider
        self.file_editor = file_editor
        self.logger = logger

    async def run(
        self,
        payload: QueryPyreflyHostPayload,
        run_context: code_action.RunActionContext[QueryPyreflyHostPayload],
    ) -> QueryPyreflyHostResult:
        self.logger.debug(
            f"Run {run_context.run_id}: answering {payload.query.kind} for"
            f" {payload.identity.project_path}"
        )
        self.pyrefly_lsp_service.register_project(payload.identity)
        root_uri = (
            self.project_info_provider.get_current_project_dir_path().as_uri()
        )
        await self.pyrefly_lsp_service.ensure_started(root_uri)
        await self.pyrefly_lsp_service.ensure_forwarded(
            payload.identity, sweep_paths=[]
        )
        raw = await self._answer(payload.query)
        return QueryPyreflyHostResult(raw=raw)

    async def _answer(
        self, query: PyreflyPositionQuery | PyreflyRangeQuery | PyreflyItemQuery
    ) -> dict[str, Any] | list[Any] | None:
        service = self.pyrefly_lsp_service

        if isinstance(query, PyreflyPositionQuery):
            file_path = resource_uri_to_path(query.file)
            position = {
                "line": query.position_line,
                "character": query.position_character,
            }
            kind = query.kind
            if kind == PyreflyQueryKind.HOVER:
                return await service.get_hover(file_path, query.content, position)
            if kind == PyreflyQueryKind.DEFINITION:
                return await service.get_definition(file_path, query.content, position)
            if kind == PyreflyQueryKind.REFERENCES:
                return await service.get_references(
                    file_path,
                    query.content,
                    position,
                    include_declaration=query.include_declaration,
                )
            if kind == PyreflyQueryKind.TYPE_DEFINITION:
                return await service.get_type_definition(
                    file_path, query.content, position
                )
            if kind == PyreflyQueryKind.IMPLEMENTATION:
                return await service.get_implementation(
                    file_path, query.content, position
                )
            if kind == PyreflyQueryKind.DOCUMENT_HIGHLIGHT:
                return await service.get_document_highlight(
                    file_path, query.content, position
                )
            if kind == PyreflyQueryKind.CALL_HIERARCHY_PREPARE:
                return await service.get_call_hierarchy_prepare(
                    file_path, query.content, position
                )
            if kind == PyreflyQueryKind.TYPE_HIERARCHY_PREPARE:
                return await service.get_type_hierarchy_prepare(
                    file_path, query.content, position
                )
            raise code_action.ActionFailedException(
                f"Unsupported pyrefly position query kind: {kind}"
            )

        if isinstance(query, PyreflyRangeQuery):
            file_path = resource_uri_to_path(query.file)
            kind = query.kind
            if kind == PyreflyQueryKind.SEMANTIC_TOKENS:
                range_dict: dict[str, Any] | None = None
                if query.start_line is not None:
                    range_dict = {
                        "start": {
                            "line": query.start_line,
                            "character": query.start_character,
                        },
                        "end": {
                            "line": query.end_line,
                            "character": query.end_character,
                        },
                    }
                return await service.get_semantic_tokens(
                    file_path, query.content, range_dict=range_dict
                )
            if kind == PyreflyQueryKind.INLAY_HINTS:
                if (
                    query.start_line is None
                    or query.start_character is None
                    or query.end_line is None
                    or query.end_character is None
                ):
                    raise code_action.ActionFailedException(
                        "Inlay-hint queries must carry a complete range"
                    )
                return await service.get_inlay_hints(
                    file_path,
                    query.content,
                    {
                        "start": {
                            "line": query.start_line,
                            "character": query.start_character,
                        },
                        "end": {
                            "line": query.end_line,
                            "character": query.end_character,
                        },
                    },
                )
            raise code_action.ActionFailedException(
                f"Unsupported pyrefly range query kind: {kind}"
            )

        item = dict(query.item)
        file_uri = item.get("uri")
        if not isinstance(file_uri, str):
            raise code_action.ActionFailedException(
                "A pyrefly hierarchy item must carry its document uri"
            )
        file_path = resource_uri_to_path(ResourceUri(file_uri))
        content = await self._read_file(file_path)
        kind = query.kind
        if kind == PyreflyQueryKind.CALL_HIERARCHY_INCOMING:
            return await service.get_call_hierarchy_incoming_calls(
                file_path, content, item
            )
        if kind == PyreflyQueryKind.CALL_HIERARCHY_OUTGOING:
            return await service.get_call_hierarchy_outgoing_calls(
                file_path, content, item
            )
        if kind == PyreflyQueryKind.TYPE_HIERARCHY_SUPERTYPES:
            return await service.get_type_hierarchy_supertypes(
                file_path, content, item
            )
        if kind == PyreflyQueryKind.TYPE_HIERARCHY_SUBTYPES:
            return await service.get_type_hierarchy_subtypes(
                file_path, content, item
            )
        raise code_action.ActionFailedException(
            f"Unsupported pyrefly item query kind: {kind}"
        )

    async def _read_file(self, file_path: Path) -> str:
        async with (
            self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session,
            session.read_file(file_path) as file_info,
        ):
            return file_info.content
