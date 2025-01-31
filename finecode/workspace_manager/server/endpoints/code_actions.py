from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def document_code_action(
    ls: LanguageServer, params: types.CodeActionParams
) -> types.CodeActionResult:
    return []


async def code_action_resolve(
    ls: LanguageServer, params: types.CodeAction
) -> types.CodeAction: ...
