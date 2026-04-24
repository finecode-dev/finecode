import asyncio
import collections.abc
import dataclasses
import inspect
import time
import typing

import cattrs
import deepmerge
from loguru import logger

from finecode_extension_runner._converter import converter as _converter

from finecode_extension_api import code_action, textstyler, service
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner import (
    context,
    domain,
    er_wal,
    global_state,
    partial_result_sender as partial_result_sender_module,
    run_utils,
    schemas,
)
from finecode_extension_runner.di import resolver as di_resolver
from finecode_extension_runner.di.registry import Registry

last_run_id: int = 0
partial_result_sender: partial_result_sender_module.PartialResultSender
handler_config_merger = deepmerge.Merger(
    [(list, ["override"]), (dict, ["merge"]), (set, ["override"])],
    #  all other types:
    ["override"],
    # strategies in the case where the types conflict:
    ["override"],
)


class ActionFailedException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class StopWithResponse(Exception):
    def __init__(self, response: schemas.RunActionResponse) -> None:
        self.response = response


class _TrackingPartialResultSender:
    """Wraps partial_result_sender.schedule_sending with state tracking."""

    def __init__(
        self,
        token: int | str,
        send_func: collections.abc.Callable[
            [int | str, code_action.RunActionResult], collections.abc.Awaitable[None]
        ],
    ) -> None:
        self._token = token
        self._send_func = send_func
        self.has_sent = False

    async def send(self, result: code_action.RunActionResult) -> None:
        self.has_sent = True
        await self._send_func(self._token, result)


class _AccumulatingPartialResultSender:
    """Collects every sent result so non-streaming callers get a final accumulated result.

    Used in place of ``_NOOP_SENDER`` when there is no ``partial_result_token``.
    Handlers that call ``partial_result_sender.send()`` directly (instead of using
    ``partial_result_scheduler``) produce a correct final result for non-streaming callers.
    """

    def __init__(self) -> None:
        self.accumulated: code_action.RunActionResult | None = None

    async def send(self, result: code_action.RunActionResult) -> None:
        if self.accumulated is None:
            self.accumulated = result
        else:
            self.accumulated.update(result)

    @property
    def has_sent(self) -> bool:
        return self.accumulated is not None


def set_partial_result_sender(send_func: typing.Callable) -> None:
    global partial_result_sender
    partial_result_sender = partial_result_sender_module.PartialResultSender(
        sender=send_func, wait_time_ms=300
    )


progress_sender_func: typing.Callable | None = None


def set_progress_sender(send_func: typing.Callable) -> None:
    global progress_sender_func
    progress_sender_func = send_func


class _ERProgressSender:
    """Concrete ProgressSender that sends notifications via the ER LSP server."""

    def __init__(
        self,
        token: int | str,
        send_func: typing.Callable[[int | str, dict], None],
    ) -> None:
        self._token = token
        self._send_func = send_func

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        self._send_func(self._token, {
            "type": "begin",
            "title": title,
            "message": message,
            "percentage": percentage,
            "cancellable": cancellable,
            "total": total,
        })

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        self._send_func(self._token, {
            "type": "report",
            "message": message,
            "percentage": percentage,
        })

    async def end(self, message: str | None = None) -> None:
        self._send_func(self._token, {
            "type": "end",
            "message": message,
        })


class AsyncPlaceholderContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb): ...


async def run_action(
    action_def: domain.ActionDeclaration,
    payload: code_action.RunActionPayload | None,
    meta: code_action.RunActionMeta,
    runner_context: context.RunnerContext,
    partial_result_token: int | str | None = None,
    progress_token: int | str | None = None,
    run_id: int | None = None,
    partial_result_queue: asyncio.Queue | None = None,
    caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    initial_result: code_action.RunActionResult | None = None,
) -> code_action.RunActionResult | None:
    # design decisions:
    # - keep payload unchanged between all subaction runs.
    #   For intermediate data use run_context
    # - result is modifiable. Result of each subaction updates the previous result.
    #   In case of failure of subaction, at least result of all previous handlers is
    #   returned. (experimental)
    # - execution of handlers can be concurrent or sequential. But executions of handler
    #   on iterable payloads(single parts) are always concurrent.

    wal_run_id = meta.wal_run_id

    if run_id is None:
        global last_run_id
        run_id = last_run_id
        last_run_id += 1

    logger.trace(
        f"run_action: action='{action_def.name}', run_id={run_id}, partial_result_token={partial_result_token}"
    )

    start_time = time.time_ns()

    try:
        action_cache = runner_context.action_cache_by_name[action_def.name]
    except KeyError:
        action_cache = domain.ActionCache()
        runner_context.action_cache_by_name[action_def.name] = action_cache

    if action_cache.exec_info is not None:
        action_exec_info = action_cache.exec_info
    else:
        action_exec_info = create_action_exec_info(action_def)
        action_cache.exec_info = action_exec_info

    execute_handlers_concurrently = (
        action_exec_info.handler_execution == code_action.HandlerExecution.CONCURRENT
    )

    run_context: code_action.RunActionContext | AsyncPlaceholderContext
    run_context_info = code_action.RunContextInfoProvider(is_concurrent_execution=execute_handlers_concurrently)
    accumulating_sender: _AccumulatingPartialResultSender | None
    if partial_result_token is not None:
        tracking_sender = _TrackingPartialResultSender(
            token=partial_result_token,
            send_func=partial_result_sender.schedule_sending,
        )
        context_sender: code_action.PartialResultSender = tracking_sender
        accumulating_sender = None
    else:
        tracking_sender = None
        accumulating_sender = _AccumulatingPartialResultSender()
        context_sender = accumulating_sender

    if progress_token is not None and progress_sender_func is not None:
        er_progress_sender: code_action.ProgressSender = _ERProgressSender(
            token=progress_token,
            send_func=progress_sender_func,
        )
    else:
        er_progress_sender = code_action._NOOP_PROGRESS_SENDER

    if action_exec_info.run_context_type is not None:
        known_args: dict[str, typing.Callable[[typing.Any], typing.Any]] = {
            "run_id": lambda _: run_id,
            "initial_payload": lambda _: payload,
            "meta": lambda _: meta,
            "info_provider": lambda _: run_context_info,
            "partial_result_sender": lambda _: context_sender,
            "progress_sender": lambda _: er_progress_sender,
        }
        known_args["caller_kwargs"] = lambda _: caller_kwargs
        constructor_args = await resolve_func_args_with_di(
            action_exec_info.run_context_type.__init__,
            known_args=known_args,
            params_to_ignore=["self"],
            registry=runner_context.di_registry,
        )

        # developers can change run context constructor, handle all exceptions
        try:
            run_context = action_exec_info.run_context_type(**constructor_args)
        except Exception as exception:
            raise ActionFailedException(
                f"Failed to instantiate run context of action {action_def.name}(Run {run_id}): {str(exception)}."
                + " See ER logs for more details"
            ) from exception
    else:
        # TODO: check run_context below, whether AsyncPlaceholder can really be used
        run_context = AsyncPlaceholderContext()

    action_result: code_action.RunActionResult | None = initial_result
    if initial_result is not None:
        run_context_info.update(initial_result)

    # to be able to catch source of exceptions in user-accessible code more precisely,
    # manually enter and exit run context
    try:
        run_context_instance = await run_context.__aenter__()
    except Exception as exception:
        raise ActionFailedException(
            f"Failed to enter run context of action {action_def.name}(Run {run_id}): {str(exception)}."
            + " See ER logs for more details"
        ) from exception

    try:
        send_partial_results = partial_result_token is not None
        logger.trace(f"R{run_id} | send_partial_results={send_partial_results}, partial_result_token={partial_result_token}, payload_type={type(payload).__name__}, is_iterable={isinstance(payload, collections.abc.AsyncIterable)}")
        with action_exec_info.process_executor.activate():
            # action payload can be iterable or not
            if isinstance(payload, collections.abc.AsyncIterable):
                # Iterable payload: handlers may either
                #   (a) call partial_result_scheduler.schedule() — classic path, or
                #   (b) call partial_result_sender.send() directly — dispatch-style path,
                #   (c) return a final result explicitly from run().
                # After all handlers run we check which path was taken and handle accordingly.
                # Priority for final result: explicit return > accumulated sends > scheduler.
                logger.trace(
                    f"R{run_id} | Iterable payload, execute all handlers to schedule coros"
                )
                handler_results: list[code_action.RunActionResult | None] = []
                for handler in action_def.handlers:
                    handler_result = await execute_action_handler(
                        action_name=action_def.name,
                        handler=handler,
                        payload=payload,
                        run_context=run_context_instance,
                        run_id=run_id,
                        action_cache=action_cache,
                        action_exec_info=action_exec_info,
                        runner_context=runner_context,
                        partial_result_token=partial_result_token,
                        wal_run_id=wal_run_id,
                        trigger=meta.trigger,
                        dev_env=meta.dev_env,
                        tracking_sender=tracking_sender,
                        partial_result_queue=partial_result_queue,
                    )
                    handler_results.append(handler_result)

                explicit_results = [r for r in handler_results if r is not None]
                handler_used_direct_sends = (
                    (tracking_sender is not None and tracking_sender.has_sent) or
                    (accumulating_sender is not None and accumulating_sender.has_sent)
                )

                if explicit_results:
                    # Handler returned a final result — use it directly.
                    for result in explicit_results:
                        if action_result is None:
                            action_result = result
                        else:
                            action_result.update(result)
                    if send_partial_results:
                        await partial_result_sender.send_all_immediately()
                elif handler_used_direct_sends:
                    # Handler sent results directly via partial_result_sender.send().
                    # Flush any buffered streaming sends; for non-streaming, surface
                    # the accumulated result.
                    logger.trace(f"R{run_id} | Handler used direct sends, skipping scheduler")
                    if send_partial_results:
                        logger.trace(f"R{run_id} | all subresults are ready, send them")
                        await partial_result_sender.send_all_immediately()
                    elif accumulating_sender is not None:
                        action_result = accumulating_sender.accumulated
                else:
                    # Classic scheduler path.
                    if not isinstance(
                        run_context_instance,
                        code_action.RunActionWithPartialResultsContext,
                    ):
                        raise ActionFailedException(
                            f"Action '{action_def.name}' uses iterable payload but run context does not provide partial_result_scheduler"
                        )
                    scheduler = run_context_instance.partial_result_scheduler

                    parts = [part async for part in payload]
                    subresults_tasks: list[asyncio.Task] = []
                    logger.trace(
                        "R{run_id} | Run subresult coros {exec_type} {partials} partial results".format(
                            run_id=run_id,
                            exec_type=(
                                "concurrently"
                                if execute_handlers_concurrently
                                else "sequentially"
                            ),
                            partials="with" if send_partial_results else "without",
                        )
                    )
                    er_wal.emit_run_event(
                        global_state.wal_writer,
                        event_type=er_wal.ErWalEventType.HANDLER_PARTS_STARTED,
                        wal_run_id=wal_run_id or "",
                        action_name=action_def.name,
                        project_path=runner_context.project.dir_path,
                        trigger=meta.trigger,
                        dev_env=meta.dev_env,
                        payload={"run_id": run_id, "part_count": len(parts)},
                    )
                    parts_start_time = time.time_ns()
                    try:
                        async with asyncio.TaskGroup() as tg:
                            for part in parts:
                                if part not in scheduler.coroutines_by_key:
                                    logger.warning(
                                        f"R{run_id} | No coroutines scheduled for part {part} "
                                        f"of action '{action_def.name}', skipping"
                                    )
                                    continue
                                part_coros = scheduler.coroutines_by_key[part]
                                del scheduler.coroutines_by_key[part]
                                if execute_handlers_concurrently:
                                    coro = run_subresult_coros_concurrently(
                                        part_coros,
                                        send_partial_results,
                                        partial_result_token,
                                        partial_result_sender,
                                        action_def.name,
                                        run_id,
                                        runner_context=runner_context,
                                        partial_result_queue=partial_result_queue,
                                        tracking_sender=tracking_sender,
                                        wal_run_id=wal_run_id,
                                        trigger=meta.trigger,
                                        dev_env=meta.dev_env,
                                    )
                                else:
                                    coro = run_subresult_coros_sequentially(
                                        part_coros,
                                        send_partial_results,
                                        partial_result_token,
                                        partial_result_sender,
                                        action_def.name,
                                        run_id,
                                        runner_context=runner_context,
                                        partial_result_queue=partial_result_queue,
                                        tracking_sender=tracking_sender,
                                        wal_run_id=wal_run_id,
                                        trigger=meta.trigger,
                                        dev_env=meta.dev_env,
                                    )
                                subresult_task = tg.create_task(coro)
                                subresults_tasks.append(subresult_task)
                    except ExceptionGroup as eg:
                        errors: list[str] = []
                        for exc in eg.exceptions:
                            if not isinstance(exc, ActionFailedException):
                                logger.error("Unexpected exception:")
                                logger.exception(exc)
                            else:
                                errors.append(exc.message)
                        raise ActionFailedException(
                            f"Running action handlers of '{action_def.name}' failed(Run {run_id}): {errors}."
                            " See ER logs for more details"
                        ) from eg
                    er_wal.emit_run_event(
                        global_state.wal_writer,
                        event_type=er_wal.ErWalEventType.HANDLER_PARTS_COMPLETED,
                        wal_run_id=wal_run_id or "",
                        action_name=action_def.name,
                        project_path=runner_context.project.dir_path,
                        trigger=meta.trigger,
                        dev_env=meta.dev_env,
                        payload={
                            "run_id": run_id,
                            "part_count": len(parts),
                            "duration_ms": (time.time_ns() - parts_start_time) / 1_000_000,
                        },
                    )

                    if send_partial_results:
                        # all subresults are ready
                        logger.trace(f"R{run_id} | all subresults are ready, send them")
                        await partial_result_sender.send_all_immediately()
                    else:
                        for subresult_task in subresults_tasks:
                            result = subresult_task.result()
                            if result is not None:
                                if action_result is None:
                                    action_result = result
                                else:
                                    action_result.update(result)
            else:
                # action payload not iterable, just execute handlers on the whole payload
                if execute_handlers_concurrently:
                    handlers_tasks: list[asyncio.Task] = []
                    try:
                        async with asyncio.TaskGroup() as tg:
                            for handler in action_def.handlers:
                                handler_task = tg.create_task(
                                    execute_action_handler(
                                        action_name=action_def.name,
                                        handler=handler,
                                        payload=payload,
                                        run_context=run_context_instance,
                                        run_id=run_id,
                                        action_cache=action_cache,
                                        action_exec_info=action_exec_info,
                                        runner_context=runner_context,
                                        partial_result_token=partial_result_token,
                                        wal_run_id=wal_run_id,
                                        trigger=meta.trigger,
                                        dev_env=meta.dev_env,
                                        tracking_sender=tracking_sender,
                                        partial_result_queue=partial_result_queue,
                                    )
                                )
                                handlers_tasks.append(handler_task)
                    except ExceptionGroup as eg:
                        for exc in eg.exceptions:
                            # TODO: expected / unexpected?
                            logger.exception(exc)
                        raise ActionFailedException(
                            f"Running action handlers of '{action_def.name}' failed"
                            f"(Run {run_id}). See ER logs for more details"
                        ) from eg

                    for handler_task in handlers_tasks:
                        coro_result = handler_task.result()
                        if coro_result is not None:
                            if action_result is None:
                                action_result = coro_result
                            else:
                                action_result.update(coro_result)
                else:
                    for handler in action_def.handlers:
                        try:
                            handler_result = await execute_action_handler(
                                action_name=action_def.name,
                                handler=handler,
                                payload=payload,
                                run_context=run_context_instance,
                                run_id=run_id,
                                action_cache=action_cache,
                                action_exec_info=action_exec_info,
                                runner_context=runner_context,
                                partial_result_token=partial_result_token,
                                wal_run_id=wal_run_id,
                                trigger=meta.trigger,
                                dev_env=meta.dev_env,
                                tracking_sender=tracking_sender,
                                partial_result_queue=partial_result_queue,
                            )
                        except ActionFailedException as exception:
                            raise exception

                        if handler_result is not None:
                            if action_result is None:
                                action_result = handler_result
                            else:
                                action_result.update(handler_result)

                            run_context_info.update(action_result)

                # Surface results sent via run_context.partial_result_sender.send()
                # (accumulated in accumulating_sender) — these are not captured by
                # handler return values but must contribute to the final result.
                if accumulating_sender is not None and accumulating_sender.has_sent:
                    if action_result is None:
                        action_result = accumulating_sender.accumulated
                    elif accumulating_sender.accumulated is not None:
                        action_result.update(accumulating_sender.accumulated)
    finally:
        # exit run context
        try:
            await run_context_instance.__aexit__(None, None, None)
        except Exception as exception:
            raise ActionFailedException(
                f"Failed to exit run context of action {action_def.name}(Run {run_id}): {str(exception)}."
                + " See ER logs for more details"
            ) from exception

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(
        f"R{run_id} | Run action end '{action_def.name}', duration: {duration}ms"
    )

    if tracking_sender is not None and tracking_sender.has_sent:
        er_wal.emit_run_event(
            global_state.wal_writer,
            event_type=er_wal.ErWalEventType.PARTIAL_RESULT_FINAL_SENT,
            wal_run_id=wal_run_id,
            action_name=action_def.name,
            project_path=runner_context.project.dir_path,
            trigger=meta.trigger,
            dev_env=meta.dev_env,
            payload={"run_id": run_id},
        )

    # if partial results were sent, `action_result` may be None
    if action_result is not None and not isinstance(
        action_result, code_action.RunActionResult
    ):
        logger.error(
            f"R{run_id} | Unexpected result type: {type(action_result).__name__}"
        )
        raise ActionFailedException(
            f"Unexpected result type: {type(action_result).__name__}"
        )

    if partial_result_queue is not None and action_result is not None:
        await partial_result_queue.put(action_result)
        return None

    return action_result


async def run_action_raw(
    request: schemas.RunActionRequest,
    options: schemas.RunActionOptions,
    runner_context: context.RunnerContext,
) -> schemas.RunActionResponse:
    global last_run_id
    run_id = last_run_id
    last_run_id += 1
    logger.trace(
        f"Run action '{request.action_name}', run id: {run_id}, partial result token: {options.partial_result_token}"
    )

    project_def = runner_context.project

    try:
        action = project_def.actions[request.action_name]
    except KeyError as exception:
        logger.error(f"R{run_id} | Action {request.action_name} not found")
        raise ActionFailedException(
            f"R{run_id} | Action {request.action_name} not found"
        ) from exception

    action_name = request.action_name

    try:
        action_cache = runner_context.action_cache_by_name[action_name]
    except KeyError:
        action_cache = domain.ActionCache()
        runner_context.action_cache_by_name[action_name] = action_cache

    if action_cache.exec_info is not None:
        action_exec_info = action_cache.exec_info
    else:
        action_exec_info = create_action_exec_info(action)
        action_cache.exec_info = action_exec_info

    # TODO: catch validation errors
    payload: code_action.RunActionPayload | None = None
    if action_exec_info.payload_type is not None:
        payload = typing.cast(
            code_action.RunActionPayload,
            _converter.structure(request.params, action_exec_info.payload_type),
        )

    wal_run_id = getattr(options, "wal_run_id", None)
    if not isinstance(wal_run_id, str) or wal_run_id.strip() == "":
        raise ActionFailedException("Missing required wal_run_id in run options")

    meta = dataclasses.replace(options.meta, wal_run_id=wal_run_id)

    er_wal.emit_run_event(
        global_state.wal_writer,
        event_type=er_wal.ErWalEventType.RUN_DISPATCHED,
        wal_run_id=wal_run_id,
        action_name=request.action_name,
        project_path=runner_context.project.dir_path,
        trigger=options.meta.trigger,
        dev_env=options.meta.dev_env,
        payload={"run_id": run_id},
    )

    action_result = await run_action(
        action_def=action,
        payload=payload,
        meta=meta,
        runner_context=runner_context,
        partial_result_token=options.partial_result_token,
        progress_token=options.progress_token,
        run_id=run_id,
    )

    response = action_result_to_run_action_response(
        action_result, options.result_formats
    )
    return response


def action_result_to_run_action_response(
    action_result: code_action.RunActionResult | None,
    asked_result_formats: list[typing.Literal["json"] | typing.Literal["string"]],
) -> schemas.RunActionResponse:
    result_by_format: dict[str, dict[str, typing.Any] | str] = {}
    run_return_code = code_action.RunReturnCode.SUCCESS
    if isinstance(action_result, code_action.RunActionResult):
        run_return_code = action_result.return_code
        for asked_result_format in asked_result_formats:
            if asked_result_format == "json":
                result_by_format["json"] = dataclasses.asdict(action_result)
            elif asked_result_format == "string":
                result_text = action_result.to_text()
                if isinstance(result_text, textstyler.StyledText):
                    result_by_format["styled_text_json"] = result_text.to_json()
                else:
                    result_by_format["string"] = result_text
            else:
                raise ActionFailedException(
                    f"Unsupported result format: {asked_result_format}"
                )
    return schemas.RunActionResponse(
        result_by_format=result_by_format,
        return_code=run_return_code.value,
    )


async def run_handlers_raw(
    request: schemas.RunHandlersRequest,
    options: schemas.RunActionOptions,
    runner_context: context.RunnerContext,
) -> schemas.RunHandlersResponse:
    """Execute a named subset of an action's handlers for multi-env orchestration.

    Seeds context.current_result from request.previous_result before the first
    handler runs, so sequential handlers across env boundaries see a continuous
    result chain.  Returns both the raw serialized result (for chaining) and
    formatted output (populated only when options.result_formats is non-empty).
    """
    global last_run_id
    run_id = last_run_id
    last_run_id += 1

    project_def = runner_context.project

    try:
        action = project_def.actions[request.action_name]
    except KeyError as exc:
        raise ActionFailedException(
            f"R{run_id} | Action {request.action_name} not found"
        ) from exc

    try:
        action_cache = runner_context.action_cache_by_name[request.action_name]
    except KeyError:
        action_cache = domain.ActionCache()
        runner_context.action_cache_by_name[request.action_name] = action_cache

    if action_cache.exec_info is None:
        action_cache.exec_info = create_action_exec_info(action)
    action_exec_info = action_cache.exec_info

    # Build a filtered ActionDeclaration containing only the requested handlers,
    # in the order specified by handler_names (preserves WM segment ordering).
    handler_names_ordered = request.handler_names
    handlers_by_name = {h.name: h for h in action.handlers}
    filtered_handlers = [
        handlers_by_name[name]
        for name in handler_names_ordered
        if name in handlers_by_name
    ]
    filtered_action = domain.ActionDeclaration(
        name=action.name,
        config=action.config,
        handlers=filtered_handlers,
        source=action.source,
    )

    # Reconstruct the previous segment's result so handlers see a continuous
    # context.current_result across the env boundary.
    initial_result: code_action.RunActionResult | None = None
    if request.previous_result is not None and action_exec_info.result_type is not None:
        try:
            initial_result = action_exec_info.result_type(**request.previous_result)
        except Exception as exc:
            logger.warning(
                f"R{run_id} | Could not reconstruct previous_result for "
                f"{request.action_name}: {exc}. Handlers will see no prior context."
            )

    # Build payload from params.
    payload: code_action.RunActionPayload | None = None
    if action_exec_info.payload_type is not None and request.params:
        payload = typing.cast(
            code_action.RunActionPayload,
            _converter.structure(request.params, action_exec_info.payload_type),
        )

    wal_run_id = getattr(options, "wal_run_id", None)
    if not isinstance(wal_run_id, str) or wal_run_id.strip() == "":
        raise ActionFailedException("Missing required wal_run_id in run options")

    meta = dataclasses.replace(options.meta, wal_run_id=wal_run_id)

    action_result = await run_action(
        action_def=filtered_action,
        payload=payload,
        meta=meta,
        runner_context=runner_context,
        partial_result_token=options.partial_result_token,
        progress_token=options.progress_token,
        run_id=run_id,
        initial_result=initial_result,
    )

    # Raw serialized result for chaining to the next segment.
    raw_result: dict = dataclasses.asdict(action_result) if action_result is not None else {}

    # Formatted result — only populated when the caller requests formats.
    formatted = action_result_to_run_action_response(action_result, options.result_formats)
    result_by_format: dict = formatted.result_by_format or {}

    return schemas.RunHandlersResponse(
        return_code=formatted.return_code,
        result=raw_result,
        result_by_format=result_by_format,
    )


def create_action_exec_info(action: domain.ActionDeclaration) -> domain.ActionExecInfo:
    try:
        action_type_def = run_utils.import_module_member_by_source_str(action.source)
    except Exception as e:
        logger.error(f"Error importing action type: {e}")
        raise e

    if not issubclass(action_type_def, code_action.Action):
        raise Exception(
            "Action class expected to be a subclass of finecode_extension_api.code_action.Action"
        )

    typed_action_type_def = typing.cast(
        type[code_action.Action[typing.Any, typing.Any, typing.Any]],
        action_type_def,
    )

    payload_type = typed_action_type_def.PAYLOAD_TYPE
    run_context_type = typed_action_type_def.RUN_CONTEXT_TYPE
    result_type = typed_action_type_def.RESULT_TYPE
    handler_execution = typed_action_type_def.HANDLER_EXECUTION

    # TODO: validate that classes and correct subclasses?

    action_exec_info = domain.ActionExecInfo(
        payload_type=payload_type,
        run_context_type=run_context_type,
        result_type=result_type,
        handler_execution=handler_execution,
    )
    return action_exec_info


async def resolve_func_args_with_di(
    func: typing.Callable,
    registry: Registry,
    known_args: dict[str, typing.Callable[[typing.Any], typing.Any]] | None = None,
    params_to_ignore: list[str] | None = None,
) -> dict[str, typing.Any]:
    func_parameters = inspect.signature(func).parameters
    func_annotations = inspect.get_annotations(func, eval_str=True)
    args: dict[str, typing.Any] = {}
    for param_name in func_parameters.keys():
        # default object constructor(__init__) has signature
        # __init__(self, *args, **kwargs)
        # args and kwargs have no annotation and should not be filled by DI resolver.
        # Ignore them.
        if (
            params_to_ignore is not None and param_name in params_to_ignore
        ) or func_parameters[param_name].kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        elif known_args is not None and param_name in known_args:
            param_type = func_annotations[param_name]
            # value in known args is a callable factory to instantiate param value
            args[param_name] = known_args[param_name](param_type)
        else:
            # TODO: handle errors
            param_type = func_annotations[param_name]
            param_value = await di_resolver.get_service_instance(param_type, registry)
            args[param_name] = param_value

    return args


def _get_handler_raw_config(
    handler: domain.ActionHandlerDeclaration,
    runner_context: context.RunnerContext,
) -> dict[str, typing.Any]:
    handler_global_config = runner_context.project.action_handler_configs.get(
        handler.source, None
    )
    handler_raw_config = {}
    if handler_global_config is not None:
        handler_raw_config = handler_global_config
    if handler_raw_config == {}:
        # still empty, just assign
        handler_raw_config = handler.config
    else:
        # not empty anymore, deep merge
        handler_config_merger.merge(handler_raw_config, handler.config)
    return handler_raw_config


async def ensure_handler_instantiated(
    handler: domain.ActionHandlerDeclaration,
    handler_cache: domain.ActionHandlerCache,
    action_exec_info: domain.ActionExecInfo,
    runner_context: context.RunnerContext,
) -> None:
    """Ensure handler is instantiated and initialized, populating handler_cache.

    If handler is already instantiated (handler_cache.instance is not None), this is
    a no-op. Otherwise, imports the handler class, resolves DI, instantiates it,
    calls on_initialize lifecycle hook if present, and caches the result.
    """
    if handler_cache.instance is not None:
        return

    handler_raw_config = _get_handler_raw_config(handler, runner_context)

    logger.trace(f"Load action handler {handler.name}")
    try:
        action_handler = run_utils.import_module_member_by_source_str(
            handler.source
        )
    except ModuleNotFoundError as error:
        logger.error(
            f"Source of action handler {handler.name} '{handler.source}'"
            " could not be imported"
        )
        logger.error(error)
        raise ActionFailedException(
            f"Import of action handler '{handler.name}' failed: {handler.source}"
        ) from error

    def get_handler_config(param_type):
        try:
            return _converter.structure(handler_raw_config, param_type)
        except cattrs.ClassValidationError as exception:
            raise ActionFailedException(str(exception)) from exception

    def get_process_executor(param_type):
        return action_exec_info.process_executor

    exec_info = domain.ActionHandlerExecInfo()
    # save immediately in context to be able to shutdown it if the first execution
    # is interrupted by stopping ER
    handler_cache.exec_info = exec_info
    if inspect.isclass(action_handler):
        args = await resolve_func_args_with_di(
            func=action_handler.__init__,
            known_args={
                "config": get_handler_config,
                "process_executor": get_process_executor,
            },
            params_to_ignore=["self"],
            registry=runner_context.di_registry,
        )

        if "lifecycle" in args:
            exec_info.lifecycle = args["lifecycle"]

        handler_instance = action_handler(**args)
        handler_cache.instance = handler_instance

        service_instances = [
            instance
            for instance in args.values()
            if isinstance(instance, service.Service)
        ]
        handler_cache.used_services = service_instances
        for service_instance in service_instances:
            if service_instance not in runner_context.running_services:
                runner_context.running_services[service_instance] = (
                    domain.RunningServiceInfo(used_by=[])
                )

            runner_context.running_services[service_instance].used_by.append(
                handler_instance
            )

    else:
        # handler is a plain function, not a class — nothing to instantiate
        handler_cache.exec_info = exec_info
        exec_info.status = domain.ActionHandlerExecInfoStatus.INITIALIZED
        return

    if (
        exec_info.lifecycle is not None
        and exec_info.lifecycle.on_initialize_callable is not None
    ):
        logger.trace(f"Initialize {handler.name} action handler")
        try:
            initialize_callable_result = (
                exec_info.lifecycle.on_initialize_callable()
            )
            if inspect.isawaitable(initialize_callable_result):
                await initialize_callable_result
        except Exception as e:
            logger.error(
                f"Failed to initialize action handler {handler.name}: {e}"
            )
            raise ActionFailedException(
                f"Initialisation of action handler '{handler.name}' failed: {e}"
            ) from e

    exec_info.status = domain.ActionHandlerExecInfoStatus.INITIALIZED


async def execute_action_handler(
    action_name: str,
    handler: domain.ActionHandlerDeclaration,
    payload: code_action.RunActionPayload | None,
    run_context: code_action.RunActionContext | AsyncPlaceholderContext,
    run_id: int,
    action_exec_info: domain.ActionExecInfo,
    action_cache: domain.ActionCache,
    runner_context: context.RunnerContext,
    partial_result_token: int | str | None = None,
    wal_run_id: str | None = None,
    trigger: str = "unknown",
    dev_env: str = "unknown",
    tracking_sender: _TrackingPartialResultSender | None = None,
    partial_result_queue: asyncio.Queue | None = None,
) -> code_action.RunActionResult | None:
    logger.trace(f"R{run_id} | Run {handler.name} on {str(payload)[:100]}...")
    if wal_run_id is not None:
        er_wal.emit_run_event(
            global_state.wal_writer,
            event_type=er_wal.ErWalEventType.HANDLER_STARTED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=runner_context.project.dir_path,
            trigger=trigger,
            dev_env=dev_env,
            payload={"run_id": run_id, "handler": handler.name},
        )
    if handler.name in action_cache.handler_cache_by_name:
        handler_cache = action_cache.handler_cache_by_name[handler.name]
    else:
        handler_cache = domain.ActionHandlerCache()
        action_cache.handler_cache_by_name[handler.name] = handler_cache

    start_time = time.time_ns()
    execution_result: code_action.RunActionResult | None = None

    if handler_cache.instance is not None:
        handler_instance = handler_cache.instance
        handler_run_func = handler_instance.run
        # TODO: check status of exec_info?
        logger.trace(
            f"R{run_id} | Instance of action handler {handler.name} found in cache"
        )
    else:
        await ensure_handler_instantiated(
            handler=handler,
            handler_cache=handler_cache,
            action_exec_info=action_exec_info,
            runner_context=runner_context,
        )
        if handler_cache.instance is not None:
            handler_run_func = handler_cache.instance.run
        else:
            # handler is a plain function
            action_handler = run_utils.import_module_member_by_source_str(
                handler.source
            )
            handler_run_func = action_handler

    def get_run_payload(param_type):
        return payload

    def get_run_context(param_type):
        return run_context

    # DI in `run` function is allowed only for action handlers in form of functions.
    # `run` in classes may not have additional parameters, constructor parameters should
    # be used instead. TODO: Validate?
    args = await resolve_func_args_with_di(
        func=handler_run_func,
        known_args={"payload": get_run_payload, "run_context": get_run_context},
        registry=runner_context.di_registry,
    )
    # TODO: cache parameters
    try:
        logger.trace(f"Call handler {handler.name}(run {run_id})")
        # there is also `inspect.iscoroutinefunction` but it cannot recognize coroutine
        # functions which are class methods. Use `isawaitable` on result instead.
        call_result = handler_run_func(**args)
        if inspect.isasyncgen(call_result):
            stream_result: code_action.RunActionResult | None = None
            async for partial_result in call_result:
                partial_result = typing.cast(code_action.RunActionResult, partial_result)
                # Both paths below forward the partial to a caller — they differ only
                # in transport.  partial_result_token sends to an LSP/MCP client via
                # the WM notification channel; partial_result_queue delivers to a parent
                # action handler that called run_action_iter().  These could be unified
                # into a single PartialResultForwarder abstraction in the future.
                if partial_result_token is not None:
                    if (
                        tracking_sender is not None
                        and wal_run_id is not None
                        and not tracking_sender.has_sent
                    ):
                        er_wal.emit_run_event(
                            global_state.wal_writer,
                            event_type=er_wal.ErWalEventType.PARTIAL_RESULT_FIRST_SENT,
                            wal_run_id=wal_run_id,
                            action_name=action_name,
                            project_path=runner_context.project.dir_path,
                            trigger=trigger,
                            dev_env=dev_env,
                            payload={"run_id": run_id, "handler": handler.name},
                        )
                        tracking_sender.has_sent = True
                    await partial_result_sender.schedule_sending(
                        partial_result_token, partial_result
                    )
                if partial_result_queue is not None:
                    await partial_result_queue.put(partial_result)
                if stream_result is None:
                    stream_result = typing.cast(
                        code_action.RunActionResult,
                        _converter.structure(
                            _converter.unstructure(partial_result),
                            type(partial_result),
                        ),
                    )
                else:
                    stream_result.update(partial_result)
            if partial_result_token is not None:
                await partial_result_sender.send_all_immediately()
                execution_result = None  # partials already sent
            elif partial_result_queue is not None:
                execution_result = None  # each partial already forwarded to queue
            else:
                execution_result = stream_result
        elif inspect.isawaitable(call_result):
            handler_result = await call_result
            if tracking_sender is not None and tracking_sender.has_sent:
                await partial_result_sender.send_all_immediately()
                execution_result = None
            else:
                execution_result = handler_result
        else:
            execution_result = call_result
    except Exception as exception:
        if isinstance(exception, code_action.StopActionRunWithResult):
            action_result = exception.result
            response = action_result_to_run_action_response(action_result, ["string"])
            raise StopWithResponse(response=response) from exception
        elif isinstance(
            exception, iprojectactionrunner.BaseRunActionException
        ) or isinstance(exception, code_action.ActionFailedException):
            error_str = exception.message
        else:
            logger.error("Unhandled exception in action handler:")
            error_str = str(exception)
        logger.exception(exception)
        if wal_run_id is not None:
            er_wal.emit_run_event(
                global_state.wal_writer,
                event_type=er_wal.ErWalEventType.HANDLER_FAILED,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=runner_context.project.dir_path,
                trigger=trigger,
                dev_env=dev_env,
                payload={"run_id": run_id, "handler": handler.name, "error": error_str},
            )
        raise ActionFailedException(
            f"Running action handler '{handler.name}' failed(Run {run_id}): {error_str}"
        ) from exception

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(
        f"R{run_id} | End of execution of action handler {handler.name}"
        f" on {str(payload)[:100]}..., duration: {duration}ms"
    )
    if wal_run_id is not None:
        er_wal.emit_run_event(
            global_state.wal_writer,
            event_type=er_wal.ErWalEventType.HANDLER_COMPLETED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=runner_context.project.dir_path,
            trigger=trigger,
            dev_env=dev_env,
            payload={"run_id": run_id, "handler": handler.name, "duration_ms": duration},
        )
    return execution_result


async def run_subresult_coros_concurrently(
    coros: list[collections.abc.Coroutine],
    send_partial_results: bool,
    partial_result_token: int | str | None,
    partial_result_sender: partial_result_sender_module.PartialResultSender,
    action_name: str,
    run_id: int,
    runner_context: context.RunnerContext,
    partial_result_queue: asyncio.Queue | None = None,
    tracking_sender: _TrackingPartialResultSender | None = None,
    wal_run_id: str | None = None,
    trigger: str = "unknown",
    dev_env: str = "unknown",
) -> code_action.RunActionResult | None:
    coros_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for coro in coros:
                coro_task = tg.create_task(coro)
                coros_tasks.append(coro_task)
    except ExceptionGroup as eg:
        errors_str = ""
        for exc in eg.exceptions:
            if isinstance(exc, code_action.ActionFailedException):
                errors_str += exc.message + "."
            else:
                logger.error("Unhandled exception:")
                logger.exception(exc)
                errors_str += str(exc) + "."
        raise ActionFailedException(
            f"Concurrent running action handlers of '{action_name}' failed(Run {run_id}): {errors_str}"
        ) from eg

    action_subresult: code_action.RunActionResult | None = None
    for coro_task in coros_tasks:
        coro_result = coro_task.result()
        if coro_result is not None:
            if action_subresult is None:
                # copy the first result because all further subresults will be merged
                # in it and result from action handler must stay immutable (e.g. it can
                # reference to cache)
                action_subresult_type = type(coro_result)
                action_subresult_dict = dataclasses.asdict(coro_result)
                action_subresult = typing.cast(
                    code_action.RunActionResult,
                    _converter.structure(action_subresult_dict, action_subresult_type),
                )
            else:
                action_subresult.update(coro_result)

    if partial_result_queue is not None:
        await partial_result_queue.put(action_subresult)
        return None
    elif send_partial_results:
        if action_subresult is None:
            return None
        if tracking_sender is not None and wal_run_id is not None and not tracking_sender.has_sent:
            er_wal.emit_run_event(
                global_state.wal_writer,
                event_type=er_wal.ErWalEventType.PARTIAL_RESULT_FIRST_SENT,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=runner_context.project.dir_path,
                trigger=trigger,
                dev_env=dev_env,
                payload={"run_id": run_id},
            )
            tracking_sender.has_sent = True
        assert partial_result_token is not None
        await partial_result_sender.schedule_sending(
            partial_result_token, action_subresult
        )
        return None
    else:
        return action_subresult


async def run_subresult_coros_sequentially(
    coros: list[collections.abc.Coroutine],
    send_partial_results: bool,
    partial_result_token: int | str | None,
    partial_result_sender: partial_result_sender_module.PartialResultSender,
    action_name: str,
    run_id: int,
    runner_context: context.RunnerContext,
    partial_result_queue: asyncio.Queue | None = None,
    tracking_sender: _TrackingPartialResultSender | None = None,
    wal_run_id: str | None = None,
    trigger: str = "unknown",
    dev_env: str = "unknown",
) -> code_action.RunActionResult | None:
    action_subresult: code_action.RunActionResult | None = None
    for coro in coros:
        try:
            coro_result = await coro
        except Exception as e:
            logger.error(
                f"Unhandled exception in subresult coroutine({action_name}, run {run_id}):"
            )
            logger.exception(e)
            raise ActionFailedException(
                f"Running action handlers of '{action_name}' failed(Run {run_id}): {e}"
            ) from e

        if coro_result is not None:
            if action_subresult is None:
                action_subresult = coro_result
            else:
                action_subresult.update(coro_result)

    if partial_result_queue is not None:
        await partial_result_queue.put(action_subresult)
        return None
    elif send_partial_results:
        if action_subresult is None:
            return None
        if tracking_sender is not None and wal_run_id is not None and not tracking_sender.has_sent:
            er_wal.emit_run_event(
                global_state.wal_writer,
                event_type=er_wal.ErWalEventType.PARTIAL_RESULT_FIRST_SENT,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=runner_context.project.dir_path,
                trigger=trigger,
                dev_env=dev_env,
                payload={"run_id": run_id},
            )
            tracking_sender.has_sent = True
        assert partial_result_token is not None
        await partial_result_sender.schedule_sending(
            partial_result_token, action_subresult
        )
        return None
    else:
        return action_subresult
