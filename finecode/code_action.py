# code actions are implementations of actions
from pathlib import Path
from typing import Any
from dataclasses import dataclass


class CodeAction:
    def __init__(self, config: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class LintMessage:
    filepath: Path
    line: int
    column: int
    code: str
    message: str
