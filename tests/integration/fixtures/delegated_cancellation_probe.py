"""Probe handlers for #67 delegated-cancellation integration tests.

Test-local packaging: copied into a temporary fixture-module directory and
made importable in copied ER venvs via a ``.pth`` file. Imports only the
standard library and ``finecode_extension_api``.

Every handler appends one JSON line per event to ``payload.log_path``.
Timestamps are ``time.time()`` (same host as the probe).

Parent: delegates to the leaf through ``payload.delegation`` and records the
run context's ``delegated_calls_outcome()`` read into ``payload.outcome_path``.

Leaf: ticks until ``LEAF_SECONDS``. On ``CancelledError`` it records
``cancel_seen``, then either cleans up briefly (``normal``/``lender``) and
re-raises, or swallows the cancel and keeps ticking (``stubborn``). The leaf
is a plain coroutine (no partials); the parent's ``run_action_iter`` forms
exercise the iterator close path without needing partial delivery.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time

from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iworkspaceactionrunner,
)

from finecode_extension_api import code_action

LEAF_SECONDS = 10.0
CLEANUP_SECONDS = 0.5
LENDER_HOLD_SECONDS = 1.5


def _event(log_path: str, who: str, what: str, **extra: object) -> None:
    if not log_path:
        return
    line = {"t": time.time(), "who": who, "what": what, "pid": os.getpid(), **extra}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _write_outcome(outcome_path: str, outcome: str) -> None:
    if not outcome_path:
        return
    with open(outcome_path, "w", encoding="utf-8") as f:
        f.write(outcome)


def _has_tick(log_path: str, minimum: int = 1) -> bool:
    if not log_path or not os.path.exists(log_path):
        return False
    count = 0
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                if '"what": "tick"' in line or '"what": "stubborn_tick"' in line:
                    count += 1
                    if count >= minimum:
                        return True
    except OSError:
        return False
    return False


@dataclasses.dataclass
class ProbePayload(code_action.RunActionPayload):
    log_path: str = ""
    outcome_path: str = ""
    # run_action | run_action_iter | workspace | early_close
    delegation: str = "run_action"
    # normal | stubborn | lender
    leaf_mode: str = "normal"
    # passthrough | sibling
    parent_behavior: str = "passthrough"
    nest: int = 0
    tick_here: bool = False
    lender_path: str = ""
    stubborn_extra: float = 6.0


@dataclasses.dataclass
class ProbeResult(code_action.RunActionResult):
    depths: list[int] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ProbeResult):
            self.depths = list(other.depths)


class ProbeContext(code_action.RunActionContext[ProbePayload]): ...


class ProbeParentAction(code_action.Action[ProbePayload, ProbeContext, ProbeResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = ProbeResult
    DESCRIPTION = "#67 delegated-cancellation parent."


class ProbeLeafAction(code_action.Action[ProbePayload, ProbeContext, ProbeResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = ProbeResult
    DESCRIPTION = "#67 delegated-cancellation leaf."


async def _tick_leaf(
    payload: ProbePayload, run_context: ProbeContext, who: str
) -> ProbeResult:
    log = payload.log_path
    _event(log, who, "start")
    deadline = time.monotonic() + LEAF_SECONDS
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if payload.leaf_mode == "lender" and payload.lender_path:
                if not os.path.exists(payload.lender_path):
                    _event(log, who, "lender_missing")
                    break
            _event(log, who, "tick")
    except asyncio.CancelledError:
        _event(log, who, "cancel_seen")
        if payload.leaf_mode == "stubborn":
            stubborn_deadline = time.monotonic() + payload.stubborn_extra
            while time.monotonic() < stubborn_deadline:
                await asyncio.sleep(0.1)
                _event(log, who, "stubborn_tick")
            _event(log, who, "stubborn_end")
            return ProbeResult(depths=[run_context.meta.orchestration_depth])
        if payload.leaf_mode == "lender" and payload.lender_path:
            hold_deadline = time.monotonic() + LENDER_HOLD_SECONDS
            try:
                while time.monotonic() < hold_deadline:
                    await asyncio.sleep(0.1)
                    if not os.path.exists(payload.lender_path):
                        _event(log, who, "lender_missing")
                        break
                    _event(log, who, "tick")
            except asyncio.CancelledError:
                _event(log, who, "cleanup_interrupted")
                raise
            _event(log, who, "cleanup_end")
            raise
        try:
            await asyncio.sleep(CLEANUP_SECONDS)
        except asyncio.CancelledError:
            _event(log, who, "cleanup_interrupted")
            raise
        _event(log, who, "cleanup_end")
        raise
    _event(log, who, "end")
    return ProbeResult(depths=[run_context.meta.orchestration_depth])


class ProbeParentHandler(
    code_action.ActionHandler[ProbeParentAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    ) -> None:
        self._runner = action_runner
        self._ws_runner = workspace_action_runner

    def _leaf_ref(self) -> iprojectactionrunner.ActionRef:
        return iprojectactionrunner.ActionRef(
            source="delegated_cancellation_probe.ProbeLeafAction",
            result_type=ProbeResult,
            action_type=None,
        )

    async def _delegate_once(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> None:
        if payload.tick_here:
            await _tick_leaf(payload, run_context, "parent_tick")
            return
        if payload.nest > 0:
            child = dataclasses.replace(payload, nest=payload.nest - 1)
            # Bounce to the other env: parent lives in dev_workspace, leaf in
            # dev_no_runtime, so parent->leaf and leaf->parent are WM-path hops.
            await self._runner.run_action(self._leaf_ref(), child, run_context.meta)
            return
        if payload.delegation == "run_action":
            await self._runner.run_action(self._leaf_ref(), payload, run_context.meta)
        elif payload.delegation == "run_action_iter":
            async for _ in self._runner.run_action_iter(
                self._leaf_ref(), payload, run_context.meta
            ):
                pass
        elif payload.delegation == "early_close":
            # Close before the reply without needing partials: drive the
            # iterator in a task, wait for the child to start (first tick in
            # the shared log), then cancel the pull. The iterator's cleanup
            # sends the release and cancels the child; the close never raises.
            async def _consume() -> None:
                async for _ in self._runner.run_action_iter(
                    self._leaf_ref(), payload, run_context.meta
                ):
                    pass

            pull = asyncio.ensure_future(_consume())
            deadline = time.monotonic() + 55.0
            while not _has_tick(payload.log_path):
                if time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.05)
            pull.cancel()
            try:
                await pull
            except asyncio.CancelledError:
                pass
        elif payload.delegation == "workspace":
            await self._ws_runner.run_action_in_projects(
                ProbeLeafAction, payload, run_context.meta
            )
        else:
            raise ValueError(f"unknown delegation {payload.delegation!r}")

    async def _sibling(self, payload: ProbePayload) -> None:
        # Let the delegated children start (they tick every 0.1 s once up),
        # then fail so only the parent's await is cancelled. The top-level run
        # catches the group and returns.
        deadline = time.monotonic() + 55.0
        minimum = 2 if payload.delegation == "workspace" else 1
        while not _has_tick(payload.log_path, minimum):
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        raise ValueError("sibling boom")

    def _read_outcome(self, run_context: ProbeContext) -> str:
        return run_context.delegated_calls_outcome().value

    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> ProbeResult:
        log = payload.log_path
        _event(log, "parent", "start")
        try:
            if payload.parent_behavior == "sibling":
                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self._delegate_once(payload, run_context))
                        group.create_task(self._sibling(payload))
                except BaseException as exc:  # noqa: BLE001
                    _event(
                        log,
                        "parent",
                        "sibling_caught",
                        exc_type=type(exc).__name__,
                        msg=f"{exc!r}"[:500],
                    )
                    _write_outcome(
                        payload.outcome_path, self._read_outcome(run_context)
                    )
                    return ProbeResult(depths=[run_context.meta.orchestration_depth])
            else:
                await self._delegate_once(payload, run_context)
        except asyncio.CancelledError as exc:
            _event(
                log,
                "parent",
                "cancelled",
                exc_type=type(exc).__name__,
            )
            outcome = self._read_outcome(run_context)
            _write_outcome(payload.outcome_path, outcome)
            if (
                payload.leaf_mode == "lender"
                and payload.lender_path
                and outcome == "stopped"
            ):
                await asyncio.sleep(1.0)
                try:
                    if os.path.exists(payload.lender_path):
                        os.unlink(payload.lender_path)
                except OSError:
                    pass
                _event(log, "parent", "lender_deleted")
            raise
        except BaseException as exc:  # noqa: BLE001
            _event(
                log,
                "parent",
                "error",
                exc_type=type(exc).__name__,
                msg=f"{exc!r}"[:500],
            )
            _write_outcome(payload.outcome_path, self._read_outcome(run_context))
            raise
        _event(log, "parent", "end")
        outcome = self._read_outcome(run_context)
        _write_outcome(payload.outcome_path, outcome)
        if (
            payload.leaf_mode == "lender"
            and payload.lender_path
            and outcome == "stopped"
        ):
            await asyncio.sleep(1.0)
            try:
                if os.path.exists(payload.lender_path):
                    os.unlink(payload.lender_path)
            except OSError:
                pass
            _event(log, "parent", "lender_deleted")
        return ProbeResult(depths=[run_context.meta.orchestration_depth])


class ProbeLeafHandler(
    code_action.ActionHandler[ProbeLeafAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
    ) -> None:
        self._runner = action_runner

    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> ProbeResult:
        if payload.nest > 0 and not payload.tick_here:
            child = dataclasses.replace(payload, nest=payload.nest - 1, tick_here=True)
            ref = iprojectactionrunner.ActionRef(
                source="delegated_cancellation_probe.ProbeParentAction",
                result_type=ProbeResult,
                action_type=None,
            )
            await self._runner.run_action(ref, child, run_context.meta)
            return ProbeResult(depths=[run_context.meta.orchestration_depth])
        return await _tick_leaf(payload, run_context, "leaf")
