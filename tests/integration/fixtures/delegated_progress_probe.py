"""Standalone probe handlers for delegated-progress relay composition tests.

Test-local packaging: copied into a temporary fixture-module directory and
made importable in copied ER venvs via a ``.pth`` file. Imports only the
standard library and ``finecode_extension_api``.

The parent handler always uses ``contextlib.aclosing`` and passes its own
open ``ProgressContext`` (unless told otherwise), so the child's begin,
report and end reach it as plain ``report(message=...)`` calls per the P4
mapping: a begin reports its message else its title, a report reports its
message, an end reports its message else ``Delegated progress ended``.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import os
import pathlib

from finecode_extension_api.interfaces import iprojectactionrunner

from finecode_extension_api import code_action

_BIG_MESSAGE_SIZE = 1024 * 1024


@dataclasses.dataclass
class ProbePayload(code_action.RunActionPayload):
    release_path: str = ""
    message: str = ""
    delegations: int = 0
    scenario: str = "hold"
    script: list[dict] = dataclasses.field(default_factory=list)
    flood_count: int = 0
    streaming_partials: int = 0
    with_progress: bool = True
    parent_close: str = "none"
    slow_pull_seconds: float = 0.0
    fanout: int = 1
    flaky_sink: bool = False


@dataclasses.dataclass
class ProbeResult(code_action.RunActionResult):
    depths: list[int] = dataclasses.field(default_factory=list)
    texts: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ProbeResult):
            self.depths = list(other.depths)
            self.texts = list(other.texts)


class ProbeContext(code_action.RunActionContext[ProbePayload]): ...


class ProbeParentAction(code_action.Action[ProbePayload, ProbeContext, ProbeResult]):
    PAYLOAD_TYPE = ProbePayload
    RUN_CONTEXT_TYPE = ProbeContext
    RESULT_TYPE = ProbeResult
    DESCRIPTION = "Probe parent for progress relay composition."


class ProbeLeafAction(ProbeParentAction):
    DESCRIPTION = "Probe leaf for progress relay composition."


class ProbeStreamingLeafAction(ProbeParentAction):
    DESCRIPTION = "Probe streaming leaf for progress relay composition."


async def _wait_release(release_path: str) -> None:
    release = pathlib.Path(release_path)
    async with asyncio.timeout(55):
        while not release.exists():
            await asyncio.sleep(0.01)


class _FailFirstTwoSender:
    """A ProgressSender failing its first two reports, then passing through.

    The first relayed report is the leaf's own begin, so dropping two takes
    out the begin and the first scripted report; the rest must continue.
    """

    def __init__(self, inner: code_action.ProgressSender) -> None:
        self._inner = inner
        self._failures_left = 2

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        await self._inner.begin(
            title,
            message=message,
            percentage=percentage,
            cancellable=cancellable,
            total=total,
        )

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise RuntimeError("flaky sink down")
        await self._inner.report(message=message, percentage=percentage)

    async def end(self, message: str | None = None) -> None:
        await self._inner.end(message=message)


class ProbeParentHandler(
    code_action.ActionHandler[ProbeParentAction, code_action.ActionHandlerConfig]
):
    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self._runner = action_runner

    def _target(self, payload: ProbePayload) -> tuple[str, ProbePayload]:
        if payload.delegations == 0:
            if payload.streaming_partials > 0:
                return (
                    "delegated_progress_probe.ProbeStreamingLeafAction",
                    payload,
                )
            return ("delegated_progress_probe.ProbeLeafAction", payload)
        return (
            "delegated_progress_probe.ProbeParentAction",
            dataclasses.replace(payload, delegations=payload.delegations - 1),
        )

    async def _collect_one(
        self,
        ref: iprojectactionrunner.ActionRef,
        payload: ProbePayload,
        meta: code_action.RunActionMeta,
        progress: code_action.ProgressContext | None,
    ) -> ProbeResult:
        child_depths: list[int] = []
        child_texts: list[str] = []
        iterator = self._runner.run_action_iter(ref, payload, meta, progress=progress)
        if payload.parent_close == "immediate":
            # Close without ever pulling: drive the iterator in a task, let
            # the request get in flight, then cancel the pull. The cleanup
            # sends the release (an unstarted generator would send nothing).
            async def _consume() -> None:
                async for _partial in iterator:
                    pass

            pull = asyncio.ensure_future(_consume())
            await asyncio.sleep(1.0)
            pull.cancel()
            try:
                await pull
            except asyncio.CancelledError:
                pass
            return ProbeResult(depths=[], texts=[])
        if payload.parent_close == "break":
            async for partial in iterator:
                if isinstance(partial, ProbeResult):
                    child_depths.extend(partial.depths)
                    child_texts.extend(partial.texts)
                break
            await asyncio.sleep(1.0)
            return ProbeResult(depths=child_depths, texts=child_texts)
        pulled = 0
        if payload.slow_pull_seconds > 0:
            await asyncio.sleep(payload.slow_pull_seconds)
        async with contextlib.aclosing(iterator) as stream:
            async for partial in stream:
                if isinstance(partial, ProbeResult):
                    child_depths.extend(partial.depths)
                    child_texts.extend(partial.texts)
                pulled += 1
                if payload.parent_close == "after_first" and pulled == 1:
                    break
                if payload.parent_close == "cancel_after_first" and pulled == 1:
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    await asyncio.sleep(30.0)
        return ProbeResult(depths=child_depths, texts=child_texts)

    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> ProbeResult:
        async with run_context.progress("probe parent") as progress:
            use = progress
            if payload.flaky_sink:
                # Test-only sender: fail the first relayed report so the
                # forwarder's drop-and-continue path runs over real ERs.
                inner = code_action.ProgressContext(
                    _FailFirstTwoSender(progress._sender),
                    "probe parent",
                )
                use = inner
            actual_progress = use if payload.with_progress else None
            target_source, target_payload = self._target(payload)
            target_payload = dataclasses.replace(
                target_payload,
                parent_close=payload.parent_close,
                slow_pull_seconds=0.0,
                fanout=1,
                flaky_sink=False,
            )
            ref = iprojectactionrunner.ActionRef(
                source=target_source,
                result_type=ProbeResult,
                action_type=None,
            )
            if payload.fanout > 1:
                second_payload = dataclasses.replace(
                    target_payload, message=target_payload.message + "-b"
                )
                collected = await asyncio.gather(
                    self._collect_one(
                        ref, target_payload, run_context.meta, actual_progress
                    ),
                    self._collect_one(
                        ref, second_payload, run_context.meta, actual_progress
                    ),
                )
                depths: list[int] = []
                texts: list[str] = []
                for item in collected:
                    depths.extend(item.depths)
                    texts.extend(item.texts)
            else:
                only = await self._collect_one(
                    ref, target_payload, run_context.meta, actual_progress
                )
                depths, texts = only.depths, only.texts
            own = run_context.meta.orchestration_depth
            return ProbeResult(depths=[own, *depths], texts=texts)


class ProbeLeafHandler(
    code_action.ActionHandler[ProbeLeafAction, code_action.ActionHandlerConfig]
):
    async def _run_script(
        self,
        payload: ProbePayload,
        run_context: ProbeContext,
        progress: code_action.ProgressContext,
    ) -> None:
        # Handlers can only begin/end through nested progress contexts, so
        # scripted begins push a nested context and ends pop the last one.
        # Begins carry no message and ends none either (the context managers
        # do not forward them); reports carry scripted messages.
        stack: list[code_action.ProgressContext] = []
        current = progress
        for op in payload.script:
            kind = op.get("op")
            if kind == "report":
                await current.report(message=op.get("message"))
            elif kind == "begin":
                nested = run_context.progress(op.get("title", "t"))
                await nested.__aenter__()
                stack.append(nested)
                current = nested
            elif kind == "end":
                if stack:
                    top = stack.pop()
                    await top.__aexit__(None, None, None)
                    current = stack[-1] if stack else progress
            elif kind == "flood_none":
                for _ in range(int(op.get("count", 0))):
                    await current.report(message=None)
            elif kind == "big":
                await current.report(message="X" * _BIG_MESSAGE_SIZE)
            elif kind == "raise":
                raise code_action.ActionFailedException("leaf failed")
            elif kind == "exit":
                os._exit(1)
            elif kind == "background":

                async def _late() -> None:
                    await asyncio.sleep(0.5)
                    for index in range(3):
                        await progress.report(message=f"late-{index}")

                _ = asyncio.ensure_future(_late())
        while stack:
            top = stack.pop()
            await top.__aexit__(None, None, None)

    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> ProbeResult:
        async with run_context.progress("probe leaf") as progress:
            if payload.scenario == "raise":
                await progress.report(message="before-fail")
                raise code_action.ActionFailedException("leaf failed")
            if payload.scenario == "exit":
                await progress.report(message="before-exit")
                os._exit(1)
            if payload.scenario == "background":

                async def _late() -> None:
                    await asyncio.sleep(0.5)
                    for index in range(3):
                        await progress.report(message=f"late-{index}")

                _ = asyncio.ensure_future(_late())
                return ProbeResult(depths=[run_context.meta.orchestration_depth])
            if payload.scenario == "big":
                await progress.report(message="X" * _BIG_MESSAGE_SIZE)
            elif payload.scenario == "script":
                await self._run_script(payload, run_context, progress)
                if not any(
                    isinstance(op, dict) and op.get("op") == "no_wait"
                    for op in payload.script
                ):
                    await _wait_release(payload.release_path)
            elif payload.scenario == "flood":
                # Message-less flood; the final end comes from the outer
                # context exit and is the newest owed value in the tail.
                for index in range(payload.flood_count):
                    await progress.report(message=None)
                await _wait_release(payload.release_path)
            else:
                await progress.report(message=payload.message)
                await _wait_release(payload.release_path)
            return ProbeResult(depths=[run_context.meta.orchestration_depth])


class ProbeStreamingLeafHandler(
    code_action.ActionHandler[ProbeStreamingLeafAction, code_action.ActionHandlerConfig]
):
    async def run(
        self, payload: ProbePayload, run_context: ProbeContext
    ) -> collections.abc.AsyncIterator[ProbeResult]:
        async with run_context.progress("probe streaming leaf") as progress:
            for index in range(payload.streaming_partials):
                await progress.report(message=f"stream-{index}")
                yield ProbeResult(
                    depths=[],
                    texts=[f"part-{index}"],
                )
                # A spacing wider than the ER's partial-send throttle so each
                # yield is flushed as its own notification, deterministically.
                await asyncio.sleep(0.5)
            await _wait_release(payload.release_path)
