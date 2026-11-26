"""Probe handlers for delegated partial-result correlation (issue #911).

Test-local packaging: copied into a temporary fixture-module directory and
made importable in copied ER venvs via a ``.pth`` file. Imports only the
standard library and ``finecode_extension_api``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import typing

from finecode_extension_api.interfaces import iprojectactionrunner

from finecode_extension_api import code_action


@dataclasses.dataclass
class ProbePayload(code_action.RunActionPayload):
    marker: typing.Literal["A", "B"] = "A"
    continue_path: str = ""
    finish_path: str = ""
    receipt_path: str = ""
    close_after_first: bool = False


@dataclasses.dataclass
class LeafResult(code_action.RunActionResult):
    items: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, LeafResult):
            self.items.extend(other.items)


@dataclasses.dataclass
class ParentResult(code_action.RunActionResult):
    items: list[str] = dataclasses.field(default_factory=list)
    pid: int = 0

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ParentResult):
            if type(other) is type(self):
                self.items.extend(other.items)


class ParentReceipt(typing.TypedDict):
    pid: int
    items: list[str]
    closed: bool


class LeafContext(code_action.RunActionContext[ProbePayload]): ...


class ParentAContext(code_action.RunActionContext[ProbePayload]): ...


class ParentBContext(code_action.RunActionContext[ProbePayload]): ...


class LeafAction(code_action.Action[ProbePayload, LeafContext, LeafResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = LeafContext
    RESULT_TYPE = LeafResult
    DESCRIPTION = "Probe leaf for delegated partial-result correlation."


class ParentAAction(code_action.Action[ProbePayload, ParentAContext, ParentResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ParentAContext
    RESULT_TYPE = ParentResult
    DESCRIPTION = "Probe parent A for delegated partial-result correlation."


class ParentBAction(code_action.Action[ProbePayload, ParentBContext, ParentResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ParentBContext
    RESULT_TYPE = ParentResult
    DESCRIPTION = "Probe parent B for delegated partial-result correlation."


def _write_receipt(path: str, pid: int, items: list[str], closed: bool) -> None:
    receipt: ParentReceipt = {"pid": pid, "items": list(items), "closed": closed}
    target = pathlib.Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(receipt), encoding="utf-8")
    os.replace(tmp, target)


async def _wait_for_path(path: str, timeout: float = 55.0) -> None:
    async with asyncio.timeout(timeout):
        while not pathlib.Path(path).exists():
            await asyncio.sleep(0.01)


class LeafHandler(
    code_action.ActionHandler[LeafAction, code_action.ActionHandlerConfig]
):
    async def run(self, payload: ProbePayload, run_context: LeafContext):
        from finecode_extension_runner._services import run_action as run_action_service

        marker = payload.marker
        yield LeafResult(items=[f"{marker}#0"])
        await run_action_service.partial_result_sender.send_all_immediately()
        if payload.continue_path:
            await _wait_for_path(payload.continue_path)
        for idx in range(1, 6):
            yield LeafResult(items=[f"{marker}#{idx}"])
        if payload.finish_path:
            await _wait_for_path(payload.finish_path)


def _leaf_payload_from_parent(payload: ProbePayload) -> ProbePayload:
    return ProbePayload(
        marker=payload.marker,
        continue_path=payload.continue_path,
        finish_path=payload.finish_path,
    )


async def _run_parent(
    payload: ProbePayload,
    run_context: typing.Any,
    runner: iprojectactionrunner.IProjectActionRunner,
) -> ParentResult:
    pid = os.getpid()
    items: list[str] = []
    closed = False
    ref = iprojectactionrunner.ActionRef(
        source="delegated_partial_result_probe.LeafAction",
        result_type=LeafResult,
        action_type=None,
    )
    leaf_payload = _leaf_payload_from_parent(payload)
    agen = runner.run_action_iter(ref, leaf_payload, run_context.meta)
    try:
        async for partial in agen:
            assert isinstance(partial, LeafResult)
            items.extend(partial.items)
            if payload.receipt_path:
                _write_receipt(payload.receipt_path, pid, items, False)
            if payload.close_after_first and len(items) >= 1:
                await agen.aclose()
                closed = True
                break
    finally:
        if payload.receipt_path:
            _write_receipt(payload.receipt_path, pid, items, closed)
    return ParentResult(items=list(items), pid=pid)


class ParentAHandler(
    code_action.ActionHandler[ParentAAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self._runner = action_runner

    async def run(
        self, payload: ProbePayload, run_context: ParentAContext
    ) -> ParentResult:
        return await _run_parent(payload, run_context, self._runner)


class ParentBHandler(
    code_action.ActionHandler[ParentBAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self._runner = action_runner

    async def run(
        self, payload: ProbePayload, run_context: ParentBContext
    ) -> ParentResult:
        return await _run_parent(payload, run_context, self._runner)
