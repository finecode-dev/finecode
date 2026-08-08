from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fine_lint.apply_code_actions_action import (
    CodeActionOperation,
    CreateFileOperation,
    DeleteFileOperation,
    RenameFileOperation,
    TextEditOperation,
)
from fine_lint.code_action_types import (
    CodeAction,
    DiagnosticRef,
)
from fine_lint.get_code_actions_action import (
    GetCodeActionsRunResult,
)
from fine_lint.lint_fix import (
    Position,
    Range,
    TextEdit,
)
from fine_lint.resolve_code_action_action import ResolveCodeActionRunResult
from loguru import logger
from lsprotocol import types

from finecode._converter import converter as _converter
from finecode.lsp_server import global_state, pygls_types_utils

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


def _structure_code_action_operation(value: Any, _type: Any) -> CodeActionOperation:
    """Pick the ``CodeActionOperation`` arm a wire dict describes.

    cattrs cannot derive this itself: its default disambiguator needs each arm to
    own a required field the others lack, and `CreateFileOperation` and
    `DeleteFileOperation` share `file_path` with everything else while their own
    fields both have defaults -- so it refuses the whole union, and every resolve
    that actually returned operations raised. Trying each arm in turn is no
    better, because cattrs ignores unknown keys: a delete would structure
    happily as a create.

    Discriminating fields, checked most-specific first (`TextEditOperation` and
    `DeleteFileOperation` share `file_version`; `RenameFileOperation` and
    `CreateFileOperation` share `overwrite`).
    """
    if not isinstance(value, dict):
        raise TypeError(
            f"Expected a mapping for a code action operation, got {value!r}"
        )
    if "edits" in value:
        operation_type: type = TextEditOperation
    elif "old_path" in value or "new_path" in value:
        operation_type = RenameFileOperation
    elif "overwrite" in value:
        operation_type = CreateFileOperation
    elif "file_version" in value:
        operation_type = DeleteFileOperation
    else:
        raise ValueError(
            f"Cannot tell which code action operation {sorted(value)} describes"
        )
    return _converter.structure(value, operation_type)


_converter.register_structure_hook(
    CodeActionOperation, _structure_code_action_operation
)


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


def _code_action_to_lsp(action: CodeAction, file_uri: str) -> types.CodeAction:
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
        # Opaque to the client; round-tripped unchanged on codeAction/resolve.
        # provider + action_id route the resolve request back to the owning
        # provider (design note D1); file_path is carried too because resolve
        # needs it to find the owning project, and params.data is the only
        # source of truth codeAction/resolve gets (the LSP request carries no
        # document context of its own).
        data={
            "provider": action.provider,
            "action_id": action.action_id,
            "file_path": file_uri,
        },
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
                    "start": {
                        "line": d.range.start.line,
                        "character": d.range.start.character,
                    },
                    "end": {
                        "line": d.range.end.line,
                        "character": d.range.end.character,
                    },
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
            action_source="fine_lint.GetCodeActionsAction",
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

    return [_code_action_to_lsp(action, file_uri) for action in result.actions]


async def code_action_resolve(
    _ls: LspServer, params: types.CodeAction
) -> types.CodeAction:
    data = params.data
    if not isinstance(data, dict):
        logger.debug(f"Cannot resolve code action: no routing data on {params.title!r}")
        return params

    provider = data.get("provider")
    action_id = data.get("action_id")
    file_uri = data.get("file_path")
    if (
        not isinstance(provider, str)
        or not isinstance(action_id, str)
        or not isinstance(file_uri, str)
    ):
        logger.debug(f"Cannot resolve code action: malformed routing data {data!r}")
        return params

    if global_state.wm_client is None:
        logger.error("Code action resolve requested but WM client not connected")
        return params

    file_path = pygls_types_utils.uri_str_to_path(file_uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        logger.debug(f"No project found for code action resolve: {file_path}")
        return params

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_lint.ResolveCodeActionAction",
            project=project_dir,
            params={
                "provider": provider,
                "action_id": action_id,
                "file_path": file_uri,
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:  # noqa: BLE001 - an editor must never get an error
        # where it asked for a code action; every failure degrades to the
        # unresolved action the client already has.
        logger.error(
            f"Error resolving code action {action_id!r} for {file_path}: {error}"
        )
        return params

    if response is None:
        return params

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return params

    try:
        result = _converter.structure(json_result, ResolveCodeActionRunResult)
    except Exception as error:  # noqa: BLE001 - same contract as the run_action
        # call above: an editor must never get an error where it asked for a
        # code action. A result this endpoint cannot read is no more use to the
        # client than a failed run, so it degrades the same way.
        logger.error(
            f"Cannot read resolved code action {action_id!r} for {file_path}: {error}"
        )
        return params

    if result.operations is None:
        logger.debug(f"No provider resolved code action {action_id!r}")
        return params

    # The read path still speaks `WorkspaceEdit.changes`, which is an unordered
    # map applied simultaneously. An operation list is ordered and may contain
    # file operations, so only the subset that `changes` can carry faithfully is
    # convertible: text edits alone, at most one operation per file. Anything
    # else is returned unresolved rather than flattened into edits that would
    # mean something different from what the provider asked for. Lifting this
    # needs the endpoint to emit `documentChanges` — see design note D11.
    changes: dict[str, list[types.TextEdit]] = {}
    for operation in result.operations:
        if not isinstance(operation, TextEditOperation):
            logger.debug(
                f"Cannot resolve code action {action_id!r} for an editor: it"
                f" contains a {type(operation).__name__}, which"
                " WorkspaceEdit.changes cannot express"
            )
            return params
        uri = str(operation.file_path)
        if uri in changes:
            logger.debug(
                f"Cannot resolve code action {action_id!r} for an editor: it"
                f" applies several ordered edit operations to {uri}, which"
                " WorkspaceEdit.changes cannot express"
            )
            return params
        changes[uri] = [_text_edit_to_lsp(edit) for edit in operation.edits]

    params.edit = types.WorkspaceEdit(changes=changes)
    return params
