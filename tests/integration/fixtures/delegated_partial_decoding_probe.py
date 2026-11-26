"""Probe handlers for decoding delegated partial results (issue #914).

Test-local packaging: copied into a temporary fixture-module directory and
made importable in copied ER venvs via a ``.pth`` file. Imports only the
standard library and ``finecode_extension_api``.
"""

from __future__ import annotations

import dataclasses
import typing

from finecode_extension_api.interfaces import iprojectactionrunner

from finecode_extension_api import code_action

LeafKind = typing.Literal["streaming", "required", "final"]


@dataclasses.dataclass
class ProbePayload(code_action.RunActionPayload):
    leaf: LeafKind = "streaming"


@dataclasses.dataclass
class TypedResult(code_action.RunActionResult):
    items: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, TypedResult):
            self.items.extend(other.items)


@dataclasses.dataclass
class RequiredFieldResult(code_action.RunActionResult):
    items: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, RequiredFieldResult):
            self.items.extend(other.items)


@dataclasses.dataclass
class ParentResult(code_action.RunActionResult):
    items: list[str] = dataclasses.field(default_factory=list)
    result_types: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ParentResult):
            self.items = list(other.items)
            self.result_types = list(other.result_types)


class ProbeContext(code_action.RunActionContext[ProbePayload]): ...


class ProbeParentAction(code_action.Action[ProbePayload, ProbeContext, ParentResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = ParentResult
    DESCRIPTION = "Probe parent that decodes a delegated child's partials."


class StreamingLeafAction(
    code_action.Action[ProbePayload, ProbeContext, TypedResult]
):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = TypedResult
    DESCRIPTION = "Probe leaf that streams typed partials."


class RequiredFieldLeafAction(
    code_action.Action[ProbePayload, ProbeContext, RequiredFieldResult]
):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = RequiredFieldResult
    DESCRIPTION = "Probe leaf that streams partials whose type has a required field."


class FinalLeafAction(code_action.Action[ProbePayload, ProbeContext, TypedResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = TypedResult
    DESCRIPTION = "Probe leaf that returns one final result."


_LEAVES: dict[str, type] = {
    "streaming": StreamingLeafAction,
    "required": RequiredFieldLeafAction,
    "final": FinalLeafAction,
}


class ProbeParentHandler(
    code_action.ActionHandler[ProbeParentAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self._runner = action_runner

    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> ParentResult:
        # A same-env leaf takes the runner's local path only when the ref
        # carries the action type; a leaf in another env goes through the WM.
        ref = iprojectactionrunner.ActionRef.from_type(_LEAVES[payload.leaf])
        items: list[str] = []
        result_types: list[str] = []
        async for partial in self._runner.run_action_iter(
            ref, payload, run_context.meta
        ):
            result_types.append(type(partial).__name__)
            items.extend(partial.items)
        return ParentResult(items=items, result_types=result_types)


class StreamingLeafHandler(
    code_action.ActionHandler[StreamingLeafAction, code_action.ActionHandlerConfig]
):
    async def run(self, payload: ProbePayload, run_context: ProbeContext):
        for idx in range(3):
            yield TypedResult(items=[f"streaming#{idx}"])


class RequiredFieldLeafHandler(
    code_action.ActionHandler[
        RequiredFieldLeafAction, code_action.ActionHandlerConfig
    ]
):
    async def run(self, payload: ProbePayload, run_context: ProbeContext):
        for idx in range(3):
            yield RequiredFieldResult(items=[f"required#{idx}"])


class FinalLeafHandler(
    code_action.ActionHandler[FinalLeafAction, code_action.ActionHandlerConfig]
):
    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> TypedResult:
        return TypedResult(items=["final#0"])
