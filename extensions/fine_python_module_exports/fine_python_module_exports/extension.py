from __future__ import annotations

import ast
from pathlib import Path

from fine_python_ast import iast_provider
from fine_python_module_exports import api

from finecode_extension_api import common_types
from finecode_extension_api.actions.ide import text_document_inlay_hint

# from finecode_extension_api import code_action

# from finecode.extension_runner.interfaces import icache

def uri_str_to_path(uri_str: str) -> Path:
    return Path(uri_str.replace("file://", ""))


async def get_document_inlay_hints(
    payload: text_document_inlay_hint.InlayHintPayload,
    ast_provider: iast_provider.IPythonSingleAstProvider,
    # cache: icache.ICache
) -> text_document_inlay_hint.InlayHintResult:
    """
    ~~It's cheap enough to calculate access type for the whole file, so calculate and
    cache. Then get nodes and access types for asked range.~~
    """
    # use uri?
    file_path: Path = uri_str_to_path(payload.text_document.uri)
    try:
        module_ast: ast.Module = await ast_provider.get_file_ast(file_path=file_path)
    except SyntaxError:
        return text_document_inlay_hint.InlayHintResult(hints=[])
    # mypy_file_revision: str = ast_provider.get_ast_revision(file_ast=mypy_file)

    # try:
    #     # cache.get_file_cache(file_path, mypy_file_revision)
    #     ... # TODO: get from cache
    # except icache.CacheMissException:

    exported_members_names: list[str] | None = api.find_exported_members_names(
        module_ast=module_ast
    )
    module_members_with_access_type = api.get_module_members_with_access_type(
        module_ast, exported_members_names, range_in_doc=payload.range
    )

    return text_document_inlay_hint.InlayHintResult(
        hints=[
            text_document_inlay_hint.InlayHint(
                position=common_types.Position(
                    line=stmt.lineno, character=stmt.col_offset
                ),
                label=(
                    "private"
                    if access_level == api.ModuleMemberAccessType.PRIVATE
                    else "public"
                ),
                kind=text_document_inlay_hint.InlayHintKind.TYPE,
                padding_right=True,
            )
            for stmt, access_level in module_members_with_access_type.items()
            if access_level != api.ModuleMemberAccessType.UNKNOWN
        ]
    )


def get_document_code_actions(): ...
