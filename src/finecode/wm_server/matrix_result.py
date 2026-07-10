"""Variant-keyed run result — the container a matrix run produces.

Maps each interpreter to that interpreter's own unmodified action result, and
is itself a `RunActionResult` so it renders and reports uniformly. No
orchestration, no running of actions, no serialization concerns — those
belong to the matrix-runner slice.
"""

from __future__ import annotations

import dataclasses
import typing

from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode_extension_api import textstyler
from finecode_extension_api.code_action import RunActionResult, RunReturnCode

__all__ = ["VariantKeyedRunResult"]

RType = typing.TypeVar("RType", bound=RunActionResult)


@dataclasses.dataclass
class VariantKeyedRunResult(RunActionResult, typing.Generic[RType]):
    variants: dict[Interpreter, RType]

    def update(self, other: RunActionResult) -> None:
        if not isinstance(other, VariantKeyedRunResult):
            return

        for interpreter, result in other.variants.items():
            if interpreter in self.variants:
                self.variants[interpreter].update(result)
            else:
                self.variants[interpreter] = result

    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()

        if not self.variants:
            text.append("No interpreters were run.\n")
            return text

        for interpreter, result in self.variants.items():
            text.append_styled(f"{interpreter.canonical}\n", bold=True)
            variant_text = result.to_text()
            if isinstance(variant_text, str):
                text.append(variant_text)
            else:
                text.text_parts.extend(variant_text.text_parts)

        return text

    @property
    def return_code(self) -> RunReturnCode:
        if any(
            result.return_code == RunReturnCode.ERROR
            for result in self.variants.values()
        ):
            return RunReturnCode.ERROR
        return RunReturnCode.SUCCESS
