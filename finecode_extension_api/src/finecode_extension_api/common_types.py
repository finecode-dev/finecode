from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Position:
    line: int
    character: int


@dataclasses.dataclass
class Range:
    start: Position
    end: Position
