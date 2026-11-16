from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import typing
from collections.abc import Awaitable, Callable
from typing import Any

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from loguru import logger

from finecode_extension_runner import (
    coverage_sink,
    domain,
    er_errors,
    er_telemetry,
    run_context,
    run_utils,
    work_slots,
)
from finecode_extension_runner.impls import _delegated_progress
from finecode_extension_runner._converter import converter as _converter
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner._services.run_action import _serialize_caller_kwargs

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class _ProgressContextForwarder:
    """Adapt a child handler's ProgressSender to the caller's ProgressContext.

    Child begin/report/end map to parent ``report`` calls only, per the shared
    P4 table, always with ``percentage=None``: the parent owns its own
    begin/end lifecycle. One instance serves one delegated call; forwarded
    calls are serialized in child-call order by ``_lock``, which also lets
    the iterator's cleanup wait out an in-flight relayed report.
    """

    def __init__(self, target: code_action.ProgressContext) -> None:
        self._target = target
        self._lock = asyncio.Lock()
        self._closed = False

    def close(self) -> None:
        """Stop starting new relayed calls. Synchronous so cleanup sets it first."""
        self._closed = True

    async def drain(self) -> None:
        """Wait for a relayed ``report`` already in flight to return."""
        async with self._lock:
            pass

    async def _forward(self, event: dict[str, object]) -> None:
        async with self._lock:
            if self._closed:
                return
            message = _delegated_progress.delegated_progress_message(event)
            if message is None:
                # Only malformed begins reach here locally: message-less
                # reports are silently owed nothing, and the typed sender
                # cannot produce unknown kinds.
                logger.warning(
                    "Dropping malformed delegated progress event: {!r}", event
                )
                return
            try:
                await self._target.report(message=message, percentage=None)
            except Exception:
                logger.warning(
                    "Dropping delegated progress report after sink failure"
                )
                return

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        _ = (percentage, cancellable, total)
        await self._forward({"type": "begin", "message": message, "title": title})

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        _ = percentage
        await self._forward({"type": "report", "message": message})

    async def end(self, message: str | None = None) -> None:
        await self._forward({"type": "end", "message": message})


def _progress_has_subscriber(progress: code_action.ProgressContext) -> bool:
    """Whether *progress*'s sender chain ends at a real subscriber.

    A context rooted at the no-op sender (or at a forwarder wrapping one,
    from nested local delegation) subscribes to nothing, so the local branch
    passes the child no forwarder and the WM path sends no ``progressToken``.
    """
    # ER-internal access to ProgressContext's private sender; precedent:
    # _services/run_action.py reads framework-private senders the same way.
    sender: object = progress._sender
    while isinstance(sender, _ProgressContextForwarder):
        sender = sender._target._sender
    return sender is not code_action._NOOP_PROGRESS_SENDER


# WM streaming queue protocol for run_action_iter (WM path):
# - dict[str, Any]: partial payload forwarded from $/progress.
# - _REPORTS_AVAILABLE: wake-up marker meaning the call's pending-report
#   deque holds messages to deliver (arrival order, oldest-first discard at
#   the per-call bound). One marker suffices no matter how many reports are
#   pending; the delivery loop drains the whole deque per wake-up.
# - ("final", raw_result): terminal item from finecode/runActionInProject response.
#   In streaming mode the final response is completion metadata only
#   (currently just returnCode); partial result payloads are sent exclusively
#   through $/progress notifications.
# - ("error", exc): terminal item when WM request itself fails.
WmRawResult = dict[str, Any]

_WORK_DONE_PREFIX = "work-done:"


@dataclasses.dataclass(frozen=True)
class _ReportsAvailable:
    """Wake-up marker: pending delegated reports wait in the call's deque."""


WmQueueItem = (
    dict[str, Any]
    | _ReportsAvailable
    | tuple[typing.Literal["final"], WmRawResult]
    | tuple[typing.Literal["error"], Exception]
)

_SENTINEL = object()
_REPORTS_AVAILABLE = _ReportsAvailable()
_last_wm_token: int = 0
_wm_partial_result_queues: dict[int, asyncio.Queue[WmQueueItem]] = {}
_wm_delegated_progress_tokens: dict[str, int] = {}


@dataclasses.dataclass
class _PendingReports:
    """Per-call delegated-report backlog, separate from partials and terminal."""

    messages: collections.deque[str] = dataclasses.field(
        default_factory=collections.deque
    )
    wake_queued: bool = False


_wm_pending_reports: dict[int, _PendingReports] = {}


def _consume_release_outcome(task: asyncio.Task[typing.Any]) -> None:
    """Retrieve a release task's outcome so it is never reported as unretrieved."""
    if not task.cancelled():
        _ = task.exception()


def dispatch_partial_result_from_wm(token: int | str, value: dict[str, Any]) -> None:
    """Called by er_server when a $/progress notification arrives from the WM.

    Values on a registered work-progress token are mapped to a parent report
    message at receipt; only messages owed a report are kept, at most 100
    pending per call with oldest-first discard. Malformed or unknown values
    are dropped with a warning; message-less reports are dropped silently.
    Values on unknown work-progress tokens (late, released) are dropped
    silently. All other tokens keep the existing partial-result dispatch.
    """
    if isinstance(token, str):
        partial_token = _wm_delegated_progress_tokens.get(token)
        if partial_token is not None:
            queue = _wm_partial_result_queues.get(partial_token)
            pending = _wm_pending_reports.get(partial_token)
            if queue is None or pending is None:
                return
            if not isinstance(value, dict):
                logger.warning(
                    "Dropping malformed delegated progress value: {!r}", value
                )
                return
            message = _delegated_progress.delegated_progress_message(value)
            if message is None:
                if value.get("type") != "report":
                    logger.warning(
                        "Dropping malformed delegated progress value: {!r}", value
                    )
                return
            pending.messages.append(message)
            while len(pending.messages) > (
                _delegated_progress.DELEGATED_PROGRESS_PENDING_LIMIT
            ):
                pending.messages.popleft()
            if not pending.wake_queued:
                pending.wake_queued = True
                queue.put_nowait(_REPORTS_AVAILABLE)
            return
        if token.startswith(_WORK_DONE_PREFIX):
            return
    queue = _wm_partial_result_queues.get(token)  # type: ignore[arg-type]
    if queue is not None:
        queue.put_nowait(value)


class ProjectActionRunnerImpl(iprojectactionrunner.IProjectActionRunner):
    """Calls the WM back-channel finecode/runActionInProject.

    Optimisation: when all handlers for the requested action are registered in
    the current env, the WM round-trip is skipped and the action is executed
    directly via the local run_action function.
    """

    def __init__(
        self,
        send_request_to_wm: Callable[[str, dict], Awaitable[Any]],
        run_action_func: Callable[
            ..., collections.abc.Awaitable[code_action.RunActionResult | None]
        ],
        actions_getter: Callable[[], dict[str, domain.ActionDeclaration]],
        current_env_name_getter: Callable[[], str],
    ) -> None:
        self._send = send_request_to_wm
        self._run_action_func = run_action_func
        self._actions_getter = actions_getter
        self._current_env_name_getter = current_env_name_getter
        self._source_cls_cache: dict[str, type] = {}

    def _find_local_action(self, action_type: type) -> domain.ActionDeclaration | None:
        return next(
            (
                a
                for a in self._actions_getter().values()
                if self._resolve_type(a.source) is action_type
            ),
            None,
        )

    def _all_handlers_in_current_env(
        self, action_def: domain.ActionDeclaration
    ) -> bool:
        env = self._current_env_name_getter()
        return bool(action_def.handlers) and all(
            h.env == env for h in action_def.handlers
        )

    def _resolve_type(self, source: str) -> type | None:
        """Import and return the class for *source*, or None on failure."""
        cls = self._source_cls_cache.get(source)
        if cls is None:
            try:
                cls = run_utils.import_module_member_by_source_str(source)
            except Exception as exception:
                logger.debug(f"Failed to import type {source}: {exception}")
                return None
            self._source_cls_cache[source] = cls
        return cls

    async def get_actions_for_parent(
        self, parent_action_type: type
    ) -> dict[str, iprojectactionrunner.ActionRef]:
        """Discover the subactions registered for *parent_action_type*.

        Per ADR-0045, this ER's own action list only ever reflects what its
        own env executes, so it can only answer for the subset of subactions
        that happen to live there; the full cross-env picture is always asked
        of the WM, which owns that topology.

        Subactions found locally still get an ``ActionRef`` carrying the
        resolved class (``action_type`` set): that lets a later ``run_action``
        call on the *same* ref execute in-process, skipping its own WM
        round-trip. This local resolution doesn't reduce the query below to a
        single call — the WM is always asked, since only it can say whether
        subactions exist in envs other than this one — it only saves the
        separate round-trip a subsequent run would otherwise need.
        """
        parent_canonical = (
            f"{parent_action_type.__module__}.{parent_action_type.__qualname__}"
        )
        result: dict[str, iprojectactionrunner.ActionRef] = {}

        # Resolve subactions that happen to live in this same env directly, so
        # the ActionRef carries a real action_type for run_action's own,
        # separate fast path (see docstring above).
        for action_def in self._actions_getter().values():
            cls = self._resolve_type(action_def.source)
            if (
                cls is None
                or getattr(cls, "PARENT_ACTION", None) is not parent_action_type
            ):
                continue
            lang = getattr(cls, "LANGUAGE", None)
            if lang is None:
                continue
            if lang in result:
                logger.error(
                    "Multiple subactions registered for language {!r} under parent"
                    " {!r}: {!r} and {!r}. Only one subaction per language is"
                    " allowed; {!r} will be ignored.",
                    lang,
                    parent_canonical,
                    result[lang],
                    cls,
                    result[lang],
                )
                continue
            result[lang] = iprojectactionrunner.ActionRef(
                source=action_def.source,
                result_type=cls.RESULT_TYPE,
                action_type=cls,
            )

        # Everything not resolved locally may live in another env. A WM
        # communication failure here must not be swallowed into "no more
        # subactions" — that would silently misreport missing subactions as
        # nonexistent ones, the exact failure mode this discovery path exists
        # to avoid. Surface it as a typed error instead.
        try:
            raw = await self._send(
                "finecode/getActionsForParent",
                {"parentActionSource": parent_canonical},
            )
        except er_errors.WmCommunicationCancelled as exc:
            raise iprojectactionrunner.ActionRunCancelled(exc.message) from exc
        except er_errors.WmCommunicationError as exc:
            raise iprojectactionrunner.ActionRunFailed(exc.message) from exc

        for sub in raw.get("subactions", []):
            lang = sub.get("language")
            canonical_source = sub.get("canonicalSource")
            if lang is None or lang in result or canonical_source is None:
                # Already resolved locally, unusable without a language tag, or
                # (shouldn't happen: find_subactions_for_parent only returns
                # actions it already resolved) missing a canonical source.
                continue
            result[lang] = iprojectactionrunner.ActionRef(
                # finecode/runActionInProject requires the canonical source, not
                # the config alias — the alias may be a re-exported path that
                # only resolves in the env owning the class (docs/wm-er-protocol.md).
                source=canonical_source,
                result_type=parent_action_type.RESULT_TYPE,
            )

        return result

    def _build_result(
        self,
        action_type: iprojectactionrunner.ActionRef,
        raw_result: dict[str, Any],
    ) -> ResultT:
        return typing.cast(
            ResultT,
            _converter.structure(raw_result, action_type.result_type),
        )

    def _coerce_payload(
        self, action_type: iprojectactionrunner.ActionRef, payload: PayloadT
    ) -> PayloadT:
        """Build the payload instance *action_type*'s own class expects.

        Implements the payload-side half of the ``ActionRef`` contract
        documented on the class itself: callers build payloads against
        whatever type they know (often a parent action's payload type when
        ``action_type`` came from ``get_actions_for_parent``), and this
        reconstructs the concrete ``PAYLOAD_TYPE`` when it's locally known and
        different, so call sites never branch on it themselves.
        """
        concrete = action_type.action_type
        if concrete is None or payload is None:
            return payload
        payload_type = getattr(concrete, "PAYLOAD_TYPE", None)
        if payload_type is None or isinstance(payload, payload_type):
            return payload
        return typing.cast(PayloadT, payload_type(**dataclasses.asdict(payload)))

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ResultT:
        work_slots.ensure_outside_scope(f"dispatching action {action_type.source}")
        payload = self._coerce_payload(action_type, payload)
        action_source = action_type.source
        if action_type.action_type is not None:
            action_def = self._find_local_action(action_type.action_type)
            if action_def is None:
                logger.debug(f"Action not found locally: {action_type.source}")
        else:
            action_def = None

        if action_def is not None and self._all_handlers_in_current_env(action_def):
            with run_action_service.public_run_errors():
                result = await self._run_action_func(
                    action_def, payload, meta, caller_kwargs=caller_kwargs
                )
            if result is None:
                raise iprojectactionrunner.ActionRunFailed(
                    f"Action '{action_def.name}' returned no result"
                )
            # The sub-run folded its own sink into *result*; carry
            # its coverage into this (calling) run's sink.
            coverage_sink.deposit_from(result)
            return result  # type: ignore[return-value]

        if action_def is not None:
            env = self._current_env_name_getter()
            outside = [(h.name, h.env) for h in action_def.handlers if h.env != env]
            logger.debug(
                "Dispatching '{}' via WM: handlers not in current env '{}': {}",
                action_type.source,
                env,
                outside,
            )

        serialized_kwargs = (
            _serialize_caller_kwargs(caller_kwargs)
            if caller_kwargs is not None
            else None
        )
        traceparent = er_telemetry.get_current_traceparent()
        wm_params: dict = {
            "actionSource": action_source,
            "payload": dataclasses.asdict(payload),
            "meta": {
                "trigger": meta.trigger.value,
                "devEnv": meta.dev_env.value,
                "orchestrationDepth": meta.orchestration_depth,
            },
            "traceparent": traceparent,
            # The run this dispatch belongs to: the nested run the WM starts
            # inherits this one's originating client (ADR-0082 rule 1).
            "runId": run_context.current_run_id(),
        }
        if serialized_kwargs is not None:
            wm_params["callerKwargs"] = serialized_kwargs
        try:
            raw_result = await self._send(
                "finecode/runActionInProject",
                wm_params,
            )
        except er_errors.WmCommunicationCancelled as exc:
            raise iprojectactionrunner.ActionRunCancelled(exc.message) from exc
        except er_errors.WmCommunicationError as exc:
            raise iprojectactionrunner.ActionRunFailed(exc.message) from exc
        raw_final_result = raw_result.get("result")
        if raw_final_result is None:
            raise iprojectactionrunner.ActionRunFailed(
                f"Action '{action_type.source}' returned no final result payload"
            )
        result = self._build_result(action_type, raw_final_result)
        # WM round-trip return point: coverage arrived inside the
        # serialized result; pick it up for the calling run's sink.
        coverage_sink.deposit_from(result)
        return result

    async def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
        *,
        progress: code_action.ProgressContext | None = None,
    ) -> collections.abc.AsyncIterator[ResultT]:
        work_slots.ensure_outside_scope(f"dispatching action {action_type.source}")
        global _last_wm_token

        payload = self._coerce_payload(action_type, payload)
        action_source = action_type.source
        if action_type.action_type is not None:
            action_def = self._find_local_action(action_type.action_type)
        else:
            action_def = None

        if action_def is not None and self._all_handlers_in_current_env(action_def):
            queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
            forwarder: _ProgressContextForwarder | None = None
            if progress is not None and _progress_has_subscriber(progress):
                forwarder = _ProgressContextForwarder(progress)
            progress_sender: code_action.ProgressSender | None = None
            if forwarder is not None:
                progress_sender = typing.cast(
                    code_action.ProgressSender, forwarder
                )
            task = asyncio.ensure_future(
                self._run_action_func(
                    action_def,
                    payload,
                    meta,
                    caller_kwargs=caller_kwargs,
                    partial_result_queue=queue,
                    progress_sender=progress_sender
                    if progress_sender is not None
                    else None,
                )
            )
            task.add_done_callback(lambda _: queue.put_nowait(_SENTINEL))
            try:
                while True:
                    item = await queue.get()
                    if item is _SENTINEL:
                        break
                    result = typing.cast(ResultT, item)
                    # Per yield: there is no final result object on
                    # this path, so each partial carries its own coverage forward.
                    coverage_sink.deposit_from(result)
                    yield result  # type: ignore[misc]
                with run_action_service.public_run_errors():
                    await task
            finally:
                # The child run is not cancelled (delegation is not a
                # cancellable capability): it keeps its sender, whose further
                # calls are dropped once closed. Closed is set before any
                # await so a second cancellation cannot re-enable the forwarder.
                if forwarder is not None:
                    forwarder.close()
                    await forwarder.drain()
            return

        if action_def is not None:
            env = self._current_env_name_getter()
            outside = [(h.name, h.env) for h in action_def.handlers if h.env != env]
            logger.debug(
                "Dispatching '{}' via WM: handlers not in current env '{}': {}",
                action_type.source,
                env,
                outside,
            )

        # WM path — streaming via $/progress notifications
        _last_wm_token += 1
        token = _last_wm_token
        wm_queue: asyncio.Queue[WmQueueItem] = asyncio.Queue()
        _wm_partial_result_queues[token] = wm_queue
        work_token: str | None = None
        if progress is not None and _progress_has_subscriber(progress):
            work_token = f"{_WORK_DONE_PREFIX}{token}"
            _wm_delegated_progress_tokens[work_token] = token
            _wm_pending_reports[token] = _PendingReports()

        serialized_kwargs = (
            _serialize_caller_kwargs(caller_kwargs)
            if caller_kwargs is not None
            else None
        )
        traceparent = er_telemetry.get_current_traceparent()

        async def _call_wm() -> None:
            try:
                wm_params: dict = {
                    "actionSource": action_source,
                    "payload": dataclasses.asdict(payload),
                    "meta": {
                        "trigger": meta.trigger.value,
                        "devEnv": meta.dev_env.value,
                        "orchestrationDepth": meta.orchestration_depth,
                    },
                    "partialResultToken": token,
                    "traceparent": traceparent,
                    "runId": run_context.current_run_id(),
                }
                if work_token is not None:
                    wm_params["progressToken"] = work_token
                if serialized_kwargs is not None:
                    wm_params["callerKwargs"] = serialized_kwargs
                raw_result = await self._send(
                    "finecode/runActionInProject",
                    wm_params,
                )
                # Terminal success item. Shape: ("final", {"result": {...}, "returnCode": ...}).
                wm_queue.put_nowait(("final", raw_result))
            except er_errors.WmCommunicationCancelled as exc:
                wm_queue.put_nowait(
                    ("error", iprojectactionrunner.ActionRunCancelled(exc.message))
                )
            except er_errors.WmCommunicationError as exc:
                wm_queue.put_nowait(
                    ("error", iprojectactionrunner.ActionRunFailed(exc.message))
                )
            except Exception as exc:
                # Terminal error item. Shape: ("error", Exception(...)).
                wm_queue.put_nowait(("error", exc))

        wm_task = asyncio.ensure_future(_call_wm())

        async def _deliver_pending_reports() -> None:
            assert progress is not None
            pending = _wm_pending_reports[token]
            pending.wake_queued = False
            while pending.messages:
                message = pending.messages.popleft()
                try:
                    await progress.report(message=message, percentage=None)
                except Exception:
                    logger.warning(
                        "Dropping delegated progress report after sink failure"
                    )
                    continue

        try:
            while True:
                item = await wm_queue.get()
                if isinstance(item, _ReportsAvailable):
                    if progress is None:
                        continue
                    await _deliver_pending_reports()
                    continue
                if isinstance(item, tuple):
                    # Tuple items are terminal markers from _call_wm:
                    # - ("error", Exception)
                    # - ("final", raw_result_dict)
                    # Pending reports are delivered before the iterator
                    # returns or raises (only when a registration exists;
                    # a no-op-rooted context registers nothing).
                    if work_token is not None:
                        await _deliver_pending_reports()
                    kind, value = item
                    if kind == "error":
                        raise value  # type: ignore[misc]
                    # kind == "final": WM streaming mode guarantees that result data
                    # was already delivered via partial-result notifications. The final
                    # tuple is only an end-of-stream marker plus completion metadata.
                    _ = typing.cast(WmRawResult, value)
                    break
                else:
                    # Non-tuple item: partial payload from dispatch_partial_result_from_wm.
                    result = self._build_result(action_type, item)
                    # Per yield (WM streaming path): each partial's
                    # coverage was serialized inside the partial payload; carry
                    # it into the calling run's sink.
                    coverage_sink.deposit_from(result)
                    yield result
        finally:
            _ = _wm_partial_result_queues.pop(token, None)
            if work_token is not None:
                _ = _wm_delegated_progress_tokens.pop(work_token, None)
            _ = _wm_pending_reports.pop(token, None)
            if not wm_task.done():
                await self._release_wm_call(token)
            if not wm_task.done():
                _ = wm_task.cancel()
                try:
                    await wm_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _release_wm_call(self, partial_token: int) -> None:
        """Send one release for a reply-less WM call.

        Parent-side registrations are already gone, so late arrivals are
        dropped. Any send or wait failure — including a work-slot
        scope refusing the send — only logs; the close still completes. A
        cancellation during the wait does not shorten it: the first
        ``CancelledError`` propagates after the reply or the bound.
        """
        release_task: asyncio.Task[typing.Any] = asyncio.ensure_future(
            self._send(
                "finecode/releaseDelegatedCall",
                {"partialResultToken": partial_token},
            )
        )
        release_task.add_done_callback(_consume_release_outcome)
        bound = _delegated_progress.RELEASE_WAIT_SECONDS
        deadline = asyncio.get_running_loop().time() + bound
        send_error: Exception | None = None
        timed_out = False
        first_cancel: asyncio.CancelledError | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(
                    asyncio.shield(release_task), timeout=remaining
                )
                break
            except TimeoutError:
                timed_out = True
                break
            except asyncio.CancelledError as exc:
                if first_cancel is None:
                    first_cancel = exc
            except Exception as exc:
                send_error = exc
                break
        if not release_task.done():
            _ = release_task.cancel()
        if send_error is not None:
            logger.warning(f"Delegated release send failed: {send_error}")
        elif timed_out:
            logger.warning(
                "Delegated release timed out after"
                f" {bound} s; the close still completes"
            )
        if first_cancel is not None:
            raise first_cancel
