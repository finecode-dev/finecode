from pathlib import Path
from typing import Protocol

import mypy.nodes as mypy_nodes


class IMypySingleAstProvider(Protocol):
    async def get_file_ast(self, file_path: Path) -> mypy_nodes.MypyFile: ...

    def get_ast_revision(self, file_ast: mypy_nodes.MypyFile) -> str: ...
