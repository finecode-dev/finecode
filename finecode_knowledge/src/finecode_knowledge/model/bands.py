from __future__ import annotations

import enum
import typing

__all__ = ["MANY", "Band"]


class Band(enum.StrEnum):
    DECLARED = "declared"
    SEMI_DECLARED = "semi_declared"
    DERIVED = "derived"


MANY: typing.Final[int] = -1
