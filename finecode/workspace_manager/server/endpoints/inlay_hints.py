from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def document_inlay_hint(
    ls: LanguageServer, params: types.InlayHintParams
) -> types.InlayHintResult:
    # return [types.InlayHint(position=types.Position(0, 0), label='', kind=types.InlayHintKind.)]
    return []


async def inlay_hint_resolve(
    ls: LanguageServer, params: types.InlayHint
) -> types.InlayHint | None: ...
