import collections.abc
import importlib
import inspect
import sys
import time
import types
import typing
from pathlib import Path
from typing import Any, Callable, TypeAliasType

from loguru import logger

from finecode.extension_runner import (
    bootstrap,
    context,
    domain,
    global_state,
    project_dirs,
    run_utils,
    schemas,
)
from finecode_extension_api import code_action


class ActionFailedException(Exception): ...


document_requester: Callable
document_saver: Callable
partial_result_sender: Callable


async def get_document(uri: str):
    doc = await document_requester(uri)
    return doc


async def save_document(uri: str, content: str):
    await document_saver(uri, content)


async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    project_path = Path(request.working_dir)

    actions: dict[str, domain.Action] = {}
    for action_name, action_schema_obj in request.actions.items():
        if len(action_schema_obj.actions) > 0:
            handlers: list[domain.ActionHandler] = []
            for subaction_name in action_schema_obj.actions:
                subaction_schema_obj = request.actions[subaction_name]
                handlers.append(
                    domain.ActionHandler(
                        name=subaction_name,
                        source=subaction_schema_obj.source,
                        config=request.actions_configs.get(subaction_name, {}),
                    )
                )
            action = domain.Action(
                name=action_name,
                config=request.actions_configs.get(action_name, {}),
                handlers=handlers,
                source=action_schema_obj.source,
            )
            actions[action_name] = action

    global_state.runner_context = context.RunnerContext(
        project=domain.Project(
            name=request.project_name,
            path=project_path,
            actions=actions,
        ),
    )

    # currently update_config is called only once directly after runner start. So we can
    # bootstrap here. Should be changed after adding updating configuration on the fly.
    bootstrap.bootstrap(
        get_document_func=get_document, save_document_func=save_document
    )

    return schemas.UpdateConfigResponse()


def resolve_func_args_with_di(
    func: Callable,
    known_args: dict[str, Any] | None = None,
    params_to_ignore: list[str] | None = None,
):
    func_parameters = inspect.signature(func).parameters
    func_annotations = inspect.get_annotations(func, eval_str=True)
    args: dict[str, Any] = {}
    for param_name in func_parameters.keys():
        # default object constructor(__init__) has signature __init__(self, *args, **kwargs)
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
            param_value = bootstrap.get_service_instance(param_type)
            args[param_name] = param_value

    return args


def create_action_exec_info(action: domain.Action) -> domain.ActionExecInfo:
    try:
        action_type_def = run_utils.import_module_member_by_source_str(action.source)
    except Exception as e:
        logger.error(f"Error importing action type: {e}")
        raise e

    if not isinstance(action_type_def, TypeAliasType):
        raise Exception("Action definition expected to be a type")

    action_type_alias = action_type_def.__value__

    if not isinstance(action_type_alias, typing._GenericAlias):
        raise Exception(
            "Action definition expected to be an instantiation of finecode_extension_api.code_action.Action type"
        )

    try:
        unpack_with_action = next(iter(action_type_alias))
    except StopIteration:
        raise Exception("Action type definition is invalid: no action type alias?")

    # typing.Unpack cannot used in isinstance:
    # TypeError: typing.Unpack cannot be used with isinstance()
    # if not isinstance(unpack_with_action,typing.Unpack):
    #     raise Exception("Action type definition is invalid: type alias is not unpack")

    if len(unpack_with_action.__args__) != 1:
        raise Exception("Action type definition is invalid: expected 1 Action instance")

    action_generic_alias = unpack_with_action.__args__[0]
    action_args = action_generic_alias.__args__

    if len(action_args) != 3:
        raise Exception(
            "Action type definition is invalid: Action type expects 3 arguments"
        )

    payload_type, run_context_type, _ = action_args

    # TODO: validate that classes and correct subclasses?

    action_exec_info = domain.ActionExecInfo(
        payload_type=payload_type, run_context_type=run_context_type
    )
    return action_exec_info


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    logger.trace(f"Run action '{request.action_name}'")
    # TODO: check whether config is set: this will be solved by passing initial
    # configuration as payload of initialize
    if global_state.runner_context is None:
        # TODO: raise error
        return schemas.RunActionResponse({})

    start_time = time.time_ns()
    project_def = global_state.runner_context.project

    try:
        action = project_def.actions[request.action_name]
    except KeyError:
        # TODO: raise error
        logger.error(f"Action {request.action_name} not found")
        return schemas.RunActionResponse({})

    # design decisions:
    # - keep payload unchanged between all subaction runs.
    #   For intermediate data use run_context
    # - result is modifiable. Result of each subaction updates the previous result.
    #   In case of failure of subaction, at least result of all previous subactions is
    #   returned. (experimental)

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
        payload = action_exec_info.payload_type(**request.params)

    run_context: code_action.RunActionContext | None = None
    if action_exec_info.run_context_type is not None:
        constructor_args = resolve_func_args_with_di(
            action_exec_info.run_context_type.__init__, params_to_ignore=["self"]
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

    for handler in action.handlers:
        handler_result = await execute_action_handler(
            handler=handler,
            payload=payload,
            run_context=run_context,
            action_context=action_context,
            runner_context=runner_context,
        )
        if action_result is None:
            action_result = handler_result
        else:
            try:
                action_result.update(handler_result)
            except NotImplementedError:
                ...

    result_dict = {}
    if isinstance(action_result, code_action.RunActionResult):
        result_dict = action_result.model_dump(mode="json")
    else:
        logger.error(f"Unexpected result type: {type(action_result).__name__}")
        raise ActionFailedException(
            f"Unexpected result type: {type(action_result).__name__}"
        )

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(f"Run action end '{request.action_name}', duration: {duration}ms")
    return schemas.RunActionResponse(result=result_dict)


async def execute_action_handler(
    handler: domain.ActionHandler,
    payload: code_action.RunActionPayload | None,
    run_context: code_action.RunActionContext | None,
    action_context: code_action.ActionContext,
    runner_context: context.RunnerContext,
) -> code_action.RunActionResult | None:
    logger.trace(f"Run {handler.name} on {str(payload)[:100]}...")
    start_time = time.time_ns()

    if handler.name in runner_context.action_handlers_instances_by_name:
        handler_instance = runner_context.action_handlers_instances_by_name[handler.name]
        handler_run_func = handler_instance.run
        exec_info = runner_context.action_handlers_exec_info_by_name[handler.name]
        logger.trace(f"Instance of action handler {handler.name} found in cache")
    else:
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
            return

        handler_raw_config = handler.config

        def get_handler_config(param_type):
            # TODO: validation errors
            return param_type(**handler_raw_config)

        def get_action_context(param_type):
            return action_context

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
                },
                params_to_ignore=["self"],
            )

            if "lifecycle" in args:
                exec_info.lifecycle = args["lifecycle"]

            handler_instance = action_handler(**args)
            runner_context.action_handlers_instances_by_name[handler.name] = handler_instance
            handler_run_func = handler_instance.run
        else:
            handler_run_func = action_handler

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
                logger.error(f"Failed to initialize action {handler.name}: {e}")
                return
        exec_info.status = domain.ActionHandlerExecInfoStatus.INITIALIZED

    def get_run_payload(param_type):
        return payload

    def get_run_context(param_type):
        return run_context

    args = resolve_func_args_with_di(
        func=handler_run_func,
        known_args={"payload": get_run_payload, "run_context": get_run_context},
    )
    # TODO: cache parameters
    try:
        # there is also `inspect.iscoroutinefunction` but it cannot recognize coroutine
        # functions which are class methods.
        call_result = handler_run_func(**args)
        if isinstance(call_result, collections.abc.AsyncIterator):
            end_result: code_action.RunActionResult | None = None
            partial_result_counter = 0

            # partial_result_token = (
            #     payload.partial_result_token
            #     if isinstance(payload, code_action.RunActionWithPartialResult)
            #     else None
            # )
            # send_partial_results = partial_result_token is not None
            async for partial_result in call_result:
                partial_result_counter += 1

                if end_result is None:
                    end_result = partial_result
                else:
                    end_result.update(partial_result)
            current_result = end_result
            logger.debug(
                f"Result of action handler {handler.name} consists of"
                f" {partial_result_counter} partial results"
            )
        elif inspect.isawaitable(call_result):
            current_result = await call_result
        else:
            current_result = call_result
    except Exception as e:
        logger.exception(e)
        return
        # TODO: error

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(
        f"End of execution of action handler {handler.name}"
        f" on {str(payload)[:100]}..., duration: {duration}ms"
    )
    return current_result


def reload_action(action_name: str) -> None:
    if global_state.runner_context is None:
        # TODO: raise error
        return

    project_def = global_state.runner_context.project

    try:
        action_obj = project_def.actions[action_name]
    except KeyError:
        available_actions_str = ",".join(
            [action_name for action_name in project_def.actions]
        )
        logger.warning(
            f"Action {action_name} not found."
            f" Available actions: {available_actions_str}"
        )
        # TODO: raise error
        return

    actions_to_remove = [action_name, *action_obj.handlers]

    for _action_name in actions_to_remove:
        try:
            del global_state.runner_context.action_handlers_instances_by_name[_action_name]
            logger.trace(f"Removed '{_action_name}' instance from cache")
        except KeyError:
            logger.info(
                f"Tried to reload action '{_action_name}', but it was not found"
            )

        if (
            _action_name
            in global_state.runner_context.action_handlers_exec_info_by_name
        ):
            shutdown_action_handler(
                action_handler_name=_action_name,
                exec_info=global_state.runner_context.action_handlers_exec_info_by_name[
                    _action_name
                ],
            )

        try:
            action_obj = project_def.actions[action_name]
        except KeyError:
            logger.warning(f"Definition of action {action_name} not found")
            continue

        action_source = action_obj.source
        if action_source is None:
            continue
        action_package = action_source.split(".")[0]

        loaded_package_modules = dict(
            [
                (key, value)
                for key, value in sys.modules.items()
                if key.startswith(action_package)
                and isinstance(value, types.ModuleType)
            ]
        )

        # delete references to these loaded modules from sys.modules
        for key in loaded_package_modules:
            del sys.modules[key]

        logger.trace(f"Remove modules of package '{action_package}' from cache")


def resolve_package_path(package_name: str) -> str:
    try:
        package_path = importlib.util.find_spec(
            package_name
        ).submodule_search_locations[0]
    except Exception:
        raise ValueError(f"Cannot find package {package_name}")

    return package_path


def document_did_open(document_uri: str) -> None:
    global_state.runner_context.docs_owned_by_client.append(document_uri)


def document_did_close(document_uri: str) -> None:
    global_state.runner_context.docs_owned_by_client.remove(document_uri)


def shutdown_action_handler(
    action_handler_name: str, exec_info: domain.ActionHandlerExecInfo
) -> None:
    # action handler exec info expected to exist in runner_context
    if exec_info.status == domain.ActionHandlerExecInfoStatus.SHUTDOWN:
        return

    if (
        exec_info.lifecycle is not None
        and exec_info.lifecycle.on_shutdown_callable is not None
    ):
        logger.trace(f"Shutdown {action_handler_name} action handler")
        try:
            exec_info.lifecycle.on_shutdown_callable()
        except Exception as e:
            logger.error(f"Failed to shutdown action {action_handler_name}: {e}")
    exec_info.status = domain.ActionHandlerExecInfoStatus.SHUTDOWN


def shutdown_all_action_handlers() -> None:
    logger.trace("Shutdown all action handlers")
    for (
        action_handler_name,
        exec_info,
    ) in global_state.runner_context.action_handlers_exec_info_by_name.items():
        shutdown_action_handler(
            action_handler_name=action_handler_name, exec_info=exec_info
        )


def exit_action_handler(
    action_handler_name: str, exec_info: domain.ActionHandlerExecInfo
) -> None:
    # action handler exec info expected to exist in runner_context
    if (
        exec_info.lifecycle is not None
        and exec_info.lifecycle.on_exit_callable is not None
    ):
        logger.trace(f"Exit {action_handler_name} action handler")
        try:
            exec_info.lifecycle.on_exit_callable()
        except Exception as e:
            logger.error(f"Failed to exit action {action_handler_name}: {e}")


def exit_all_action_handlers() -> None:
    logger.trace("Exit all action handlers")
    for (
        action_handler_name,
        exec_info,
    ) in global_state.runner_context.action_handlers_exec_info_by_name.items():
        exit_action_handler(
            action_handler_name=action_handler_name, exec_info=exec_info
        )
    global_state.runner_context.action_handlers_exec_info_by_name = {}
