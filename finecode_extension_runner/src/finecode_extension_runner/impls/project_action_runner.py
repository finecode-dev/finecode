from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import typing
from typing import Any, Awaitable, Callable

import apischema
from loguru import logger

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner import domain, run_utils


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
        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
        return next(
            (a for a in self._actions_getter().values() if a.source == action_source),
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
            except Exception:
                return None
            self._source_cls_cache[source] = cls
        return cls

    def get_actions_for_parent(self, parent_action_type: type) -> dict[str, type]:
        result: dict[str, type] = {}
        for action_def in self._actions_getter().values():
            cls = self._resolve_type(action_def.source)
            if cls is None:
                continue
            if getattr(cls, "PARENT_ACTION", None) is parent_action_type:
                lang = getattr(cls, "LANGUAGE", None)
                if lang is not None:
                    result[lang] = cls
        return result

    def _build_result(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        raw_result: dict[str, Any],
    ) -> ResultT:
        return typing.cast(
            ResultT,
            apischema.deserialize(action_type.RESULT_TYPE, raw_result),
        )

    async def run_action(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ResultT:
        action_def = self._find_local_action(action_type)
        if action_def is not None and self._all_handlers_in_current_env(action_def):
            result = await self._run_action_func(
                action_def, payload, meta, caller_kwargs=caller_kwargs
            )
            if result is None:
                raise iprojectactionrunner.ActionRunFailed(
                    f"Action '{action_def.name}' returned no result"
                )
            return result  # type: ignore[return-value]

        if caller_kwargs is not None:
            logger.warning(
                f"caller_kwargs passed to IProjectActionRunner.run_action for"
                f" '{action_type.__qualname__}' but the action will be dispatched"
                f" via the WM — caller_kwargs cannot cross process boundaries and"
                f" will be ignored."
                # TODO: support caller_kwargs for cross-env calls
            )

        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
        raw_result = await self._send(
            "finecode/runActionInProject",
            {
                "actionSource": action_source,
                "payload": dataclasses.asdict(payload),
                "meta": {
                    "trigger": meta.trigger.value,
                    "devEnv": meta.dev_env.value,
                    "orchestrationDepth": meta.orchestration_depth,
                },
            },
        )
        raw_final_result = raw_result.get("result")
        if raw_final_result is None:
            raise iprojectactionrunner.ActionRunFailed(
                f"Action '{action_type.__qualname__}' returned no final result payload"
            )
        return self._build_result(action_type, raw_final_result)

    async def run_action_iter(  # type: ignore[override]
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> collections.abc.AsyncIterator[ResultT]:
        global _last_wm_token

        action_def = self._find_local_action(action_type)
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

        if caller_kwargs is not None:
            logger.warning(
                f"caller_kwargs passed to IProjectActionRunner.run_action_iter for"
                f" '{action_type.__qualname__}' but the action will be dispatched"
                f" via the WM — caller_kwargs cannot cross process boundaries and"
                f" will be ignored."
                # TODO: support caller_kwargs for cross-env calls
            )

        # WM path — streaming via $/progress notifications
        _last_wm_token += 1
        token = _last_wm_token
        wm_queue: asyncio.Queue[WmQueueItem] = asyncio.Queue()
        _wm_partial_result_queues[token] = wm_queue

        action_source = f"{action_type.__module__}.{action_type.__qualname__}"

        async def _call_wm() -> None:
            try:
                raw_result = await self._send(
                    "finecode/runActionInProject",
                    {
                        "actionSource": action_source,
                        "payload": dataclasses.asdict(payload),
                        "meta": {
                            "trigger": meta.trigger.value,
                            "devEnv": meta.dev_env.value,
                            "orchestrationDepth": meta.orchestration_depth,
                        },
                        "partialResultToken": token,
                    },
                )
                # Terminal success item. Shape: ("final", {"result": {...}, "returnCode": ...}).
                wm_queue.put_nowait(("final", raw_result))
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
