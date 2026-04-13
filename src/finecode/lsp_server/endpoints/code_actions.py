from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


async def document_code_action(
    _ls: LspServer, params: types.CodeActionParams
) -> types.CodeActionResult:
    logger.debug(f"{params}")
    return [
        types.CodeAction(
            title="Make Private", kind=types.CodeActionKind.RefactorRewrite
        )
    ]


async def code_action_resolve(
    _ls: LspServer, params: types.CodeAction
) -> types.CodeAction: ...
