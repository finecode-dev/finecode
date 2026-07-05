from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import typing
from typing import Any, Awaitable, Callable

from loguru import logger

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner import domain, er_errors, er_telemetry, run_utils
from finecode_extension_runner._converter import converter as _converter


PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)

# WM streaming queue protocol for run_action_iter (WM path):
# - dict[str, Any]: partial payload forwarded from $/progress.
# - ("final", raw_result): terminal item from finecode/runActionInProject response.
#   In streaming mode the final response is completion metadata only
#   (currently just returnCode); partial result payloads are sent exclusively
#   through $/progress notifications.
# - ("error", exc): terminal item when WM request itself fails.
WmRawResult = dict[str, Any]
WmQueueItem = (
    dict[str, Any]
    | tuple[typing.Literal["final"], WmRawResult]
    | tuple[typing.Literal["error"], Exception]
)

_SENTINEL = object()
_last_wm_token: int = 0
_wm_partial_result_queues: dict[int, asyncio.Queue[WmQueueItem]] = {}


def dispatch_partial_result_from_wm(token: int | str, value: dict[str, Any]) -> None:
    """Called by er_server when a $/progress notification arrives from the WM.

    Routes the partial result dict to the asyncio.Queue registered for *token*
    by an in-flight run_action_iter WM-path call.  Silently ignored when no
    matching queue exists (e.g. stale notifications).

    The queued value is always a *partial* payload dict. Terminal queue items
    are injected only by _call_wm inside run_action_iter as tuples.
    """
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
        run_action_func: Callable[..., collections.abc.Awaitable[code_action.RunActionResult | None]],
        actions_getter: Callable[[], dict[str, domain.ActionDeclaration]],
        current_env_name_getter: Callable[[], str],
    ) -> None:
        self._send = send_request_to_wm
        self._run_action_func = run_action_func
        self._actions_getter = actions_getter
        self._current_env_name_getter = current_env_name_getter
        self._source_cls_cache: dict[str, type] = {}

    def _find_local_action(
        self, action_type: type
    ) -> domain.ActionDeclaration | None:
        return next(
            (
                a
                for a in self._actions_getter().values()
                if self._resolve_type(a.source) is action_type
            ),
            None,
        )

    def _all_handlers_in_current_env(self, action_def: domain.ActionDeclaration) -> bool:
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

    async def get_actions_for_parent(self, parent_action_type: type) -> dict[str, iprojectactionrunner.ActionRef]:
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
        parent_canonical = f"{parent_action_type.__module__}.{parent_action_type.__qualname__}"
        result: dict[str, iprojectactionrunner.ActionRef] = {}

        # Resolve subactions that happen to live in this same env directly, so
        # the ActionRef carries a real action_type for run_action's own,
        # separate fast path (see docstring above).
        for action_def in self._actions_getter().values():
            cls = self._resolve_type(action_def.source)
            if cls is None or getattr(cls, "PARENT_ACTION", None) is not parent_action_type:
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
        payload = self._coerce_payload(action_type, payload)
        action_source = action_type.source
        if action_type.action_type is not None:
            action_def = self._find_local_action(action_type.action_type)
            if action_def is None:
                logger.debug(f"Action not found locally: {action_type.source}")
        else:
            action_def = None

        if action_def is not None and self._all_handlers_in_current_env(action_def):
            result = await self._run_action_func(
                action_def, payload, meta, caller_kwargs=caller_kwargs
            )
            if result is None:
                raise iprojectactionrunner.ActionRunFailed(
                    f"Action '{action_def.name}' returned no result"
                )
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

        serialized_kwargs = _serialize_caller_kwargs(caller_kwargs) if caller_kwargs is not None else None
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
        return self._build_result(action_type, raw_final_result)

    async def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> collections.abc.AsyncIterator[ResultT]:
        global _last_wm_token

        payload = self._coerce_payload(action_type, payload)
        action_source = action_type.source
        if action_type.action_type is not None:
            action_def = self._find_local_action(action_type.action_type)
        else:
            action_def = None

        if action_def is not None and self._all_handlers_in_current_env(action_def):
            queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
            task = asyncio.ensure_future(
                self._run_action_func(
                    action_def, payload, meta,
                    caller_kwargs=caller_kwargs,
                    partial_result_queue=queue,
                )
            )
            task.add_done_callback(lambda _: queue.put_nowait(_SENTINEL))
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item  # type: ignore[misc]
            await task
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

        serialized_kwargs = _serialize_caller_kwargs(caller_kwargs) if caller_kwargs is not None else None
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
                }
                if serialized_kwargs is not None:
                    wm_params["callerKwargs"] = serialized_kwargs
                raw_result = await self._send(
                    "finecode/runActionInProject",
                    wm_params,
                )
                # Terminal success item. Shape: ("final", {"result": {...}, "returnCode": ...}).
                wm_queue.put_nowait(("final", raw_result))
            except er_errors.WmCommunicationCancelled as exc:
                wm_queue.put_nowait(("error", iprojectactionrunner.ActionRunCancelled(exc.message)))
            except er_errors.WmCommunicationError as exc:
                wm_queue.put_nowait(("error", iprojectactionrunner.ActionRunFailed(exc.message)))
            except Exception as exc:
                # Terminal error item. Shape: ("error", Exception(...)).
                wm_queue.put_nowait(("error", exc))

        wm_task = asyncio.ensure_future(_call_wm())

        try:
            while True:
                item = await wm_queue.get()
                if isinstance(item, tuple):
                    # Tuple items are terminal markers from _call_wm:
                    # - ("error", Exception)
                    # - ("final", raw_result_dict)
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
                    yield self._build_result(action_type, item)
        finally:
            _wm_partial_result_queues.pop(token, None)
            if not wm_task.done():
                wm_task.cancel()
