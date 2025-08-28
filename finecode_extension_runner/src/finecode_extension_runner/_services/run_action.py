import asyncio
import collections.abc
import dataclasses
import inspect
import time
import typing

import deepmerge
from loguru import logger
from pydantic.dataclasses import dataclass as pydantic_dataclass

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.interfaces import iactionrunner
from finecode_extension_runner import context, domain, global_state
from finecode_extension_runner import (
    partial_result_sender as partial_result_sender_module,
)
from finecode_extension_runner import project_dirs, run_utils, schemas
from finecode_extension_runner.di import resolver as di_resolver

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


def set_partial_result_sender(send_func: typing.Callable) -> None:
    global partial_result_sender
    partial_result_sender = partial_result_sender_module.PartialResultSender(
        sender=send_func, wait_time_ms=300
    )


async def run_action(
    request: schemas.RunActionRequest, options: schemas.RunActionOptions
) -> schemas.RunActionResponse:
    global last_run_id
    run_id = last_run_id
    last_run_id += 1
    logger.trace(f"Run action '{request.action_name}', run id: {run_id}")
    # TODO: check whether config is set: this will be solved by passing initial
    # configuration as payload of initialize
    if global_state.runner_context is None:
        raise ActionFailedException(
            "Run of action failed because extension runner is not initialized yet"
        )

    start_time = time.time_ns()
    project_def = global_state.runner_context.project

    try:
        action = project_def.actions[request.action_name]
    except KeyError:
        logger.error(f"R{run_id} | Action {request.action_name} not found")
        raise ActionFailedException(
            f"R{run_id} | Action {request.action_name} not found"
        )

    # design decisions:
    # - keep payload unchanged between all subaction runs.
    #   For intermediate data use run_context
    # - result is modifiable. Result of each subaction updates the previous result.
    #   In case of failure of subaction, at least result of all previous handlers is
    #   returned. (experimental)
    # - execution of handlers can be concurrent or sequential. But executions of handler
    #   on iterable payloads(single parts) are always concurrent.

    try:
        action_exec_info = global_state.runner_context.action_exec_info_by_name[
            request.action_name
        ]
    except KeyError:
        action_exec_info = create_action_exec_info(action)
        global_state.runner_context.action_exec_info_by_name[request.action_name] = (
            action_exec_info
        )

    # TODO: catch validation errors
    payload: code_action.RunActionPayload | None = None
    if action_exec_info.payload_type is not None:
        payload_type_with_validation = pydantic_dataclass(action_exec_info.payload_type)
        payload = payload_type_with_validation(**request.params)

    run_context: code_action.RunActionContext | None = None
    if action_exec_info.run_context_type is not None:
        constructor_args = resolve_func_args_with_di(
            action_exec_info.run_context_type.__init__,
            known_args={"run_id": lambda _: run_id},
            params_to_ignore=["self"],
        )

        run_context = action_exec_info.run_context_type(**constructor_args)
        # TODO: handler errors
        await run_context.init(initial_payload=payload)

    action_result: code_action.RunActionResult | None = None
    runner_context = global_state.runner_context

    # instantiate only on demand?
    project_path = project_def.path
    project_cache_dir = project_dirs.get_project_dir(project_path=project_path)
    action_context = code_action.ActionContext(
        project_dir=project_path, cache_dir=project_cache_dir
    )

    # TODO: take value from action config
    execute_handlers_concurrently = action.name == "lint"
    partial_result_token = options.partial_result_token
    send_partial_results = partial_result_token is not None
    with action_exec_info.process_executor.activate():
        # action payload can be iterable or not
        if isinstance(payload, collections.abc.AsyncIterable):
            # iterable: `run` method should not calculate results itself, but call
            #           `partial_result_scheduler.schedule`. Then we execute provided
            #           coroutines either concurrently or sequentially.
            logger.trace(
                f"R{run_id} | Iterable payload, execute all handlers to schedule coros"
            )
            for handler in action.handlers:
                await execute_action_handler(
                    handler=handler,
                    payload=payload,
                    run_context=run_context,
                    run_id=run_id,
                    action_context=action_context,
                    action_exec_info=action_exec_info,
                    runner_context=runner_context,
                )

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
            try:
                async with asyncio.TaskGroup() as tg:
                    for part in parts:
                        part_coros = (
                            run_context.partial_result_scheduler.coroutines_by_key[part]
                        )
                        del run_context.partial_result_scheduler.coroutines_by_key[part]
                        if execute_handlers_concurrently:
                            coro = run_subresult_coros_concurrently(
                                part_coros,
                                send_partial_results,
                                partial_result_token,
                                partial_result_sender,
                                action.name,
                                run_id,
                            )
                        else:
                            coro = run_subresult_coros_sequentially(
                                part_coros,
                                send_partial_results,
                                partial_result_token,
                                partial_result_sender,
                                action.name,
                                run_id,
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
                    f"Running action handlers of '{action.name}' failed(Run {run_id}): {errors}."
                    " See ER logs for more details"
                )

            if send_partial_results:
                # all subresults are ready
                logger.trace(f"R{run_id} | all subresults are ready, send them")
                await partial_result_sender.send_all_immediately()

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
                        for handler in action.handlers:
                            handler_task = tg.create_task(
                                execute_action_handler(
                                    handler=handler,
                                    payload=payload,
                                    run_context=run_context,
                                    run_id=run_id,
                                    action_context=action_context,
                                    action_exec_info=action_exec_info,
                                    runner_context=runner_context,
                                )
                            )
                            handlers_tasks.append(handler_task)
                except ExceptionGroup as eg:
                    for exc in eg.exceptions:
                        # TODO: expected / unexpected?
                        logger.exception(exc)
                    raise ActionFailedException(
                        f"Running action handlers of '{action.name}' failed"
                        f"(Run {run_id}). See ER logs for more details"
                    )

                for handler_task in handlers_tasks:
                    coro_result = handler_task.result()
                    if coro_result is not None:
                        if action_result is None:
                            action_result = coro_result
                        else:
                            action_result.update(coro_result)
            else:
                for handler in action.handlers:
                    try:
                        handler_result = await execute_action_handler(
                            handler=handler,
                            payload=payload,
                            run_context=run_context,
                            run_id=run_id,
                            action_context=action_context,
                            action_exec_info=action_exec_info,
                            runner_context=runner_context,
                        )
                    except ActionFailedException as exception:
                        raise exception

                    if handler_result is not None:
                        if action_result is None:
                            action_result = handler_result
                        else:
                            action_result.update(handler_result)

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(
        f"R{run_id} | Run action end '{request.action_name}', duration: {duration}ms"
    )

    if not isinstance(action_result, code_action.RunActionResult):
        logger.error(
            f"R{run_id} | Unexpected result type: {type(action_result).__name__}"
        )
        raise ActionFailedException(
            f"Unexpected result type: {type(action_result).__name__}"
        )

    response = action_result_to_run_action_response(
        action_result, options.result_format
    )
    return response


def action_result_to_run_action_response(
    action_result: code_action.RunActionResult,
    asked_result_format: typing.Literal["json"] | typing.Literal["string"],
) -> schemas.RunActionResponse:
    serialized_result: dict[str, typing.Any] | str | None = None
    result_format = "string"
    run_return_code = code_action.RunReturnCode.SUCCESS
    if isinstance(action_result, code_action.RunActionResult):
        run_return_code = action_result.return_code
        if asked_result_format == "json":
            serialized_result = dataclasses.asdict(action_result)
            result_format = "json"
        elif asked_result_format == "string":
            result_text = action_result.to_text()
            if isinstance(result_text, textstyler.StyledText):
                serialized_result = result_text.to_json()
                result_format = "styled_text_json"
            else:
                serialized_result = result_text
                result_format = "string"
        else:
            raise ActionFailedException(
                f"Unsupported result format: {asked_result_format}"
            )
    return schemas.RunActionResponse(
        result=serialized_result,
        format=result_format,
        return_code=run_return_code.value,
    )


def create_action_exec_info(action: domain.Action) -> domain.ActionExecInfo:
    try:
        action_type_def = run_utils.import_module_member_by_source_str(action.source)
    except Exception as e:
        logger.error(f"Error importing action type: {e}")
        raise e

    if not issubclass(action_type_def, code_action.Action):
        raise Exception(
            "Action class expected to be a subclass of finecode_extension_api.code_action.Action"
        )

    payload_type = action_type_def.PAYLOAD_TYPE
    run_context_type = action_type_def.RUN_CONTEXT_TYPE

    # TODO: validate that classes and correct subclasses?

    action_exec_info = domain.ActionExecInfo(
        payload_type=payload_type, run_context_type=run_context_type
    )
    return action_exec_info


def resolve_func_args_with_di(
    func: typing.Callable,
    known_args: dict[str, typing.Callable[[typing.Any], typing.Any]] | None = None,
    params_to_ignore: list[str] | None = None,
):
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
        ) or param_name in ["args", "kwargs"]:
            continue
        elif known_args is not None and param_name in known_args:
            param_type = func_annotations[param_name]
            # value in known args is a callable factory to instantiate param value
            args[param_name] = known_args[param_name](param_type)
        else:
            # TODO: handle errors
            param_type = func_annotations[param_name]
            param_value = di_resolver.get_service_instance(param_type)
            args[param_name] = param_value

    return args


async def execute_action_handler(
    handler: domain.ActionHandler,
    payload: code_action.RunActionPayload | None,
    run_context: code_action.RunActionContext | None,
    run_id: int,
    action_exec_info: domain.ActionExecInfo,
    action_context: code_action.ActionContext,
    runner_context: context.RunnerContext,
) -> code_action.RunActionResult:
    logger.trace(f"R{run_id} | Run {handler.name} on {str(payload)[:100]}...")
    start_time = time.time_ns()
    execution_result: code_action.RunActionResult | None = None

    if handler.name in runner_context.action_handlers_instances_by_name:
        handler_instance = runner_context.action_handlers_instances_by_name[
            handler.name
        ]
        handler_run_func = handler_instance.run
        exec_info = runner_context.action_handlers_exec_info_by_name[handler.name]
        logger.trace(
            f"R{run_id} | Instance of action handler {handler.name} found in cache"
        )
    else:
        logger.trace(f"R{run_id} | Load action handler {handler.name}")
        try:
            action_handler = run_utils.import_module_member_by_source_str(
                handler.source
            )
        except ModuleNotFoundError as error:
            logger.error(
                f"R{run_id} | Source of action handler {handler.name} '{handler.source}'"
                " could not be imported"
            )
            logger.error(error)
            raise ActionFailedException(
                f"Import of action handler '{handler.name}' failed(Run {run_id}): {handler.source}"
            )

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

        def get_handler_config(param_type):
            # TODO: validation errors
            return param_type(**handler_raw_config)

        def get_action_context(param_type):
            return action_context

        def get_process_executor(param_type):
            return action_exec_info.process_executor

        exec_info = domain.ActionHandlerExecInfo()
        # save immediately in context to be able to shutdown it if the first execution
        # is interrupted by stopping ER
        runner_context.action_handlers_exec_info_by_name[handler.name] = exec_info
        if inspect.isclass(action_handler):
            args = resolve_func_args_with_di(
                func=action_handler.__init__,
                known_args={
                    "config": get_handler_config,
                    "context": get_action_context,
                    "process_executor": get_process_executor,
                },
                params_to_ignore=["self"],
            )

            if "lifecycle" in args:
                exec_info.lifecycle = args["lifecycle"]

            handler_instance = action_handler(**args)
            runner_context.action_handlers_instances_by_name[handler.name] = (
                handler_instance
            )
            handler_run_func = handler_instance.run
        else:
            handler_run_func = action_handler

        if (
            exec_info.lifecycle is not None
            and exec_info.lifecycle.on_initialize_callable is not None
        ):
            logger.trace(f"R{run_id} | Initialize {handler.name} action handler")
            try:
                initialize_callable_result = (
                    exec_info.lifecycle.on_initialize_callable()
                )
                if inspect.isawaitable(initialize_callable_result):
                    await initialize_callable_result
            except Exception as e:
                logger.error(
                    f"R{run_id} | Failed to initialize action handler {handler.name}: {e}"
                )
                raise ActionFailedException(
                    f"Initialisation of action handler '{handler.name}' failed(Run {run_id}): {e}"
                )

        exec_info.status = domain.ActionHandlerExecInfoStatus.INITIALIZED

    def get_run_payload(param_type):
        return payload

    def get_run_context(param_type):
        return run_context

    # DI in `run` function is allowed only for action handlers in form of functions.
    # `run` in classes may not have additional parameters, constructor parameters should
    # be used instead. TODO: Validate?
    args = resolve_func_args_with_di(
        func=handler_run_func,
        known_args={"payload": get_run_payload, "run_context": get_run_context},
    )
    # TODO: cache parameters
    try:
        # there is also `inspect.iscoroutinefunction` but it cannot recognize coroutine
        # functions which are class methods. Use `isawaitable` on result instead.
        call_result = handler_run_func(**args)
        if inspect.isawaitable(call_result):
            execution_result = await call_result
        else:
            execution_result = call_result
    except Exception as exception:
        if isinstance(exception, code_action.StopActionRunWithResult):
            action_result = exception.result
            response = action_result_to_run_action_response(action_result, "string")
            raise StopWithResponse(response=response)
        elif isinstance(exception, iactionrunner.BaseRunActionException) or isinstance(
            exception, code_action.ActionFailedException
        ):
            error_str = exception.message
        else:
            logger.error("Unhandled exception in action handler:")
            logger.exception(exception)
            error_str = str(exception)
        raise ActionFailedException(
            f"Running action handler '{handler.name}' failed(Run {run_id}): {error_str}"
        )

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(
        f"R{run_id} | End of execution of action handler {handler.name}"
        f" on {str(payload)[:100]}..., duration: {duration}ms"
    )
    return execution_result


async def run_subresult_coros_concurrently(
    coros: list[collections.abc.Coroutine],
    send_partial_results: bool,
    partial_result_token: int | str,
    partial_result_sender: partial_result_sender_module.PartialResultSender,
    action_name: str,
    run_id: int,
) -> code_action.RunActionResult | None:
    coros_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for coro in coros:
                coro_task = tg.create_task(coro)
                coros_tasks.append(coro_task)
    except ExceptionGroup as eg:
        logger.error(f"R{run_id} | {eg}")
        for exc in eg.exceptions:
            logger.exception(exc)
        raise ActionFailedException(
            f"Concurrent running action handlers of '{action_name}' failed(Run {run_id}). See logs for more details"
        )

    action_subresult: code_action.RunActionResult | None = None
    for coro_task in coros_tasks:
        coro_result = coro_task.result()
        if coro_result is not None:
            if action_subresult is None:
                action_subresult = coro_result
            else:
                action_subresult.update(coro_result)

    if send_partial_results:
        await partial_result_sender.schedule_sending(
            partial_result_token, action_subresult
        )
        return None
    else:
        return action_subresult


async def run_subresult_coros_sequentially(
    coros: list[collections.abc.Coroutine],
    send_partial_results: bool,
    partial_result_token: int | str,
    partial_result_sender: partial_result_sender_module.PartialResultSender,
    action_name: str,
    run_id: int,
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
            )

        if coro_result is not None:
            if action_subresult is None:
                action_subresult = coro_result
            else:
                action_subresult.update(coro_result)

    if send_partial_results:
        await partial_result_sender.schedule_sending(
            partial_result_token, action_subresult
        )
        return None
    else:
        return action_subresult
