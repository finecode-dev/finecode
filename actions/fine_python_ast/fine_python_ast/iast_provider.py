import ast
from pathlib import Path
from typing import Protocol


class IPythonSingleAstProvider(Protocol):
    async def get_file_ast(self, file_path: Path) -> ast.Module:
        ...

    def get_ast_revision(self, file_ast: ast.Module) -> str:
        ...
