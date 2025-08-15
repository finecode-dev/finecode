from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def document_code_lens(
    ls: LanguageServer, params: types.CodeLensParams
) -> types.CodeLensResult:
    return [
        # Example:
        # types.CodeLens(
        #     range=types.Range(start=types.Position(0, 0), end=types.Position(0, 1)),
        #     command=types.Command(title="Create test", command="createTest"),
        #     data=None,
        # )
    ]


async def code_lens_resolve(
    ls: LanguageServer, params: types.CodeLens
) -> types.CodeLens:
    logger.trace(f"resolve code lens {params}")
