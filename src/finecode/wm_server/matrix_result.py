"""Variant-keyed run result — the container a matrix run produces.

Maps each interpreter to that interpreter's own unmodified action result, and
is itself a `RunActionResult` so it renders and reports uniformly. No
orchestration, no running of actions, no serialization concerns — those
belong to the matrix-runner slice.

PRD-0003 Approach A, implemented
----------------------------------------------------
`VariantKeyedRunResult` is the reference for the variant-keyed SEMANTICS
(key by interpreter, per-section text rendering, `return_code` = OR across
variants, `update()` merge-by-key). It is NOT on the execution path today:
both the non-streaming path
(`finecode.wm_server.services.run_service.matrix_runner`) and the
streaming/CLI path
(`finecode.wm_server.services.run_service.matrix_streaming`) run each
interpreter variant in its own Extension Runner process and combine their
serialized `RunActionResponse`s WM-side (nesting `result_by_format`, OR-ing
return codes), because the WM cannot import extension-defined
`RunActionResult` subclasses and so can never hold live per-variant result
objects to key with this class. This module and its unit tests remain as the
documented/verified semantics for a future evolution, not dead code to
remove.

Approach B — the forward path (documented here, not built)
------------------------------------------------------------
When a concrete cross-variant analysis need appears (e.g. an action wants to
diff results between interpreters, not just present them side by side),
Approach A's per-process isolation stops being sufficient and the following
evolution is the intended path:

1. Move `VariantKeyedRunResult` (this class) and `Interpreter` into
   `finecode_extension_api`, so an Extension Runner — not just the WM — can
   import them.
2. Add an optional, overridable hook on `RunActionResult`:
   `combine_variants(dict[Interpreter, Result]) -> RunActionResult`, with a
   generic default (equivalent to constructing a `VariantKeyedRunResult`).
3. Run that hook in a designated "aggregator" ER, which deserializes its
   peer variants' results back into live typed objects (the one place this
   is possible, since only an ER importing the action's package can
   reconstruct its `RESULT_TYPE`) and invokes the hook.
4. Actions that want interpreter-diffing behavior override `combine_variants`;
   all others keep the generic variant-keyed default — no action author is
   forced to opt in just because the matrix runner evolved.

This is intentionally not built until a concrete need for it exists.
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
