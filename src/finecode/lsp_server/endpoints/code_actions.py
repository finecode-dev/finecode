from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode._converter import converter as _converter
from finecode.lsp_server import global_state, pygls_types_utils
from finecode_extension_api.actions.code_quality.code_action_types import (
    CodeAction,
    DiagnosticRef,
)
from finecode_extension_api.actions.code_quality.get_code_actions_action import (
    GetCodeActionsRunResult,
)
from finecode_extension_api.actions.code_quality.lint_fix import (
    Position,
    Range,
    TextEdit,
)

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


def _lsp_range_to_range(r: types.Range) -> Range:
    return Range(
        start=Position(line=r.start.line, character=r.start.character),
        end=Position(line=r.end.line, character=r.end.character),
    )


def _lsp_diagnostic_to_ref(diag: types.Diagnostic) -> DiagnosticRef:
    codes: list[str] = []
    if diag.code is not None:
        codes.append(str(diag.code))
    return DiagnosticRef(
        range=_lsp_range_to_range(diag.range),
        codes=codes,
    )


def _text_edit_to_lsp(edit: TextEdit) -> types.TextEdit:
    return types.TextEdit(
        range=types.Range(
            start=types.Position(
                line=edit.range.start.line,
                character=edit.range.start.character,
            ),
            end=types.Position(
                line=edit.range.end.line,
                character=edit.range.end.character,
            ),
        ),
        new_text=edit.new_text,
    )


def _code_action_to_lsp(action: CodeAction) -> types.CodeAction:
    workspace_edit: types.WorkspaceEdit | None = None
    if action.edits is not None:
        changes: dict[str, list[types.TextEdit]] = {
            uri: [_text_edit_to_lsp(e) for e in edits]
            for uri, edits in action.edits.items()
        }
        workspace_edit = types.WorkspaceEdit(changes=changes)

    related_diagnostics: list[types.Diagnostic] | None = None
    if action.diagnostics:
        related_diagnostics = [
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(
                        line=d.range.start.line,
                        character=d.range.start.character,
                    ),
                    end=types.Position(
                        line=d.range.end.line,
                        character=d.range.end.character,
                    ),
                ),
                message="",
                code=d.codes[0] if d.codes else None,
            )
            for d in action.diagnostics
        ]

    return types.CodeAction(
        title=action.title,
        kind=action.kind if action.kind else None,
        edit=workspace_edit,
        diagnostics=related_diagnostics,
        is_preferred=action.is_preferred if action.is_preferred else None,
        data=action.action_id,
    )


async def document_code_action(
    _ls: LspServer, params: types.CodeActionParams
) -> types.CodeActionResult:
    logger.debug(f"code action: {params.text_document.uri}")

    if global_state.wm_client is None:
        logger.error("Code actions requested but WM client not connected")
        return []

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        logger.debug(f"No project found for code actions: {file_path}")
        return []

    file_uri = file_path.as_uri()
    request_range = params.range
    context = params.context

    only: list[str] | None = None
    if context.only:
        only = [k if isinstance(k, str) else k.value for k in context.only]

    diagnostics = [_lsp_diagnostic_to_ref(d) for d in (context.diagnostics or [])]

    action_params: dict[str, Any] = {
        "file_path": file_uri,
        "range": {
            "start": {
                "line": request_range.start.line,
                "character": request_range.start.character,
            },
            "end": {
                "line": request_range.end.line,
                "character": request_range.end.character,
            },
        },
        "diagnostics": [
            {
                "range": {
                    "start": {"line": d.range.start.line, "character": d.range.start.character},
                    "end": {"line": d.range.end.line, "character": d.range.end.character},
                },
                "codes": d.codes,
            }
            for d in diagnostics
        ],
    }
    if only is not None:
        action_params["only"] = only
    if context.trigger_kind is not None:
        action_params["trigger_kind"] = context.trigger_kind.value

    try:
        response = await global_state.wm_client.run_action(
            action_source="finecode_extension_api.actions.GetCodeActionsAction",
            project=project_dir,
            params=action_params,
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        logger.error(f"Error fetching code actions for {file_path}: {error}")
        return []

    if response is None:
        return []

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return []

    result = _converter.structure(json_result, GetCodeActionsRunResult)

    return [_code_action_to_lsp(action) for action in result.actions]


async def code_action_resolve(
    _ls: LspServer, params: types.CodeAction
) -> types.CodeAction:
    # v1: edits are always embedded; resolve returns unchanged action.
    return params

