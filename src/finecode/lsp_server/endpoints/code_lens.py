from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


async def document_code_lens(
    _ls: LspServer, _params: types.CodeLensParams
) -> types.CodeLensResult:
    return []


async def code_lens_resolve(
    _ls: LspServer, params: types.CodeLens
) -> types.CodeLens:
    logger.trace(f"resolve code lens {params}")
