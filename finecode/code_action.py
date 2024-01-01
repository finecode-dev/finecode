# code actions are implementations of actions
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CodeAction:
    def __init__(self, config: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class LintMessage:
    filepath: Path
    line: int
    column: int
    code: str
    message: str
