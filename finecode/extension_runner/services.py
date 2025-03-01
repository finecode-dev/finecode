import importlib
import inspect
import sys
import types
from pathlib import Path
from typing import Any, Callable, Type
import time

from loguru import logger

from finecode.extension_runner import (
    bootstrap,
    code_action,
    context,
    domain,
    global_state,
    project_dirs,
    run_utils,
    schemas,
)


class ActionFailedException(Exception): ...


document_requester: Callable
document_saver: Callable


async def get_document(uri: str):
    doc = await document_requester(uri)
    return doc


async def save_document(uri: str, content: str):
    await document_saver(uri, content)


async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    project_path = Path(request.working_dir)

    global_state.runner_context = context.RunnerContext(
        project=domain.Project(
            name=request.project_name,
            path=project_path,
            actions={
                action_name: domain.Action(
                    name=action.name, subactions=action.actions, source=action.source
                )
                for action_name, action in request.actions.items()
            },
            actions_configs=request.actions_configs,
        ),
    )

    # currently update_config is called only once directly after runner start. So we can bootstrap
    # here. Should be changed after adding updating configuration on the fly.
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
        if params_to_ignore is not None and param_name in params_to_ignore:
            continue
        elif known_args is not None and param_name in known_args:
            args[param_name] = known_args[param_name]
        else:
            # TODO: handle errors
            param_type = func_annotations[param_name]
            param_value = bootstrap.get_service_instance(param_type)
            args[param_name] = param_value

    return args


def get_action_payload_type(
    actions_to_execute: list[domain.Action],
) -> Type[code_action.RunPayloadType] | None:
    # assume all actions have the same payload type, find the first one
    for action in actions_to_execute:
        if action.source is not None:
            try:
                action_handler = run_utils.import_module_member_by_source_str(
                    action.source
                )
            except ModuleNotFoundError as error:
                logger.error(
                    f"Source of action {action.name} '{action.source}' could not be imported"
                )
                logger.error(error)
                return

            if inspect.isclass(action_handler):
                run_method = action_handler.run
            else:
                run_method = action_handler
            run_method_annotations = inspect.get_annotations(run_method, eval_str=True)
            # TODO: handle errors
            payload_type = run_method_annotations["payload"]
            return payload_type

    # action has no payload
    return None


async def instantiate_run_context(
    actions_to_execute: list[domain.Action], payload: code_action.RunPayloadType
) -> code_action.RunActionContext | None:
    # assume all actions that have run context, have it of the same type
    for action in actions_to_execute:
        if action.source is not None:
            try:
                action_handler = run_utils.import_module_member_by_source_str(
                    action.source
                )
            except ModuleNotFoundError as error:
                logger.error(
                    f"Source of action {action.name} '{action.source}' could not be imported"
                )
                logger.error(error)
                return

            if inspect.isclass(action_handler):
                run_method = action_handler.run
            else:
                run_method = action_handler
            run_method_annotations = inspect.get_annotations(run_method, eval_str=True)
            try:
                run_context_type = run_method_annotations["run_context"]
            except KeyError:
                continue

            if not issubclass(run_context_type, code_action.RunActionContext):
                raise ValueError(
                    f"Type of run_context '{run_context_type}' is not subclass of RunActionContext"
                )
            constructor_args = resolve_func_args_with_di(
                run_context_type.__init__, params_to_ignore=["self"]
            )
            run_context = run_context_type(**constructor_args)
            await run_context.init(initial_payload=payload)
            return run_context

    # action has no run context
    return None


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    logger.trace(f"Run action '{request.action_name}'")
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists
    if global_state.runner_context is None:
        # TODO: raise error
        return schemas.RunActionResponse({})

    start_time = time.time_ns()
    project_def = global_state.runner_context.project
    action_to_process: list[str] = [request.action_name]
    actions_to_execute: list[domain.Action] = []

    while len(action_to_process) > 0:
        action_name = action_to_process.pop(0)
        try:
            action_obj = project_def.actions[action_name]
        except KeyError:
            logger.warning(
                f"Action {request.action_name} not found. Available actions: {','.join([action_name for action_name in project_def.actions])}"
            )
            # TODO: raise error
            return schemas.RunActionResponse({})

        if action_obj.source is not None:
            actions_to_execute.append(action_obj)

        action_to_process += action_obj.subactions

    # design decisions:
    # - keep payload unchanged between all subaction runs. For intermediate data use run_context
    # - result is modifiable. Result of each subaction updates the previous result. In case of
    #   failure of subaction, at least result of all previous subactions is returned. (experimental)
    #   TODO: Would it be better to provide interface for intermiate results like messages from linter
    #   and make result non-modifiable?
    # TODO: cache
    payload_type = get_action_payload_type(actions_to_execute)
    if payload_type is not None:
        # TODO: handle errors
        payload = payload_type(**request.params)
    else:
        payload = None
    run_context = await instantiate_run_context(actions_to_execute, payload)
    current_result: code_action.RunActionResult | None = None
    runner_context = global_state.runner_context

    for action in actions_to_execute:
        action_result = await execute_action_handler(
            action,
            payload=payload,
            run_context=run_context,
            runner_context=runner_context,
            project_def=project_def,
        )
        if current_result is None:
            current_result = action_result
        else:
            try:
                current_result.update(action_result)
            except NotImplementedError:
                ...

    result_dict = {}
    if isinstance(current_result, code_action.RunActionResult):
        result_dict = current_result.model_dump(mode="json")
    else:
        logger.error(f"Unexpected result type: {type(current_result).__name__}")
        raise ActionFailedException(
            f"Unexpected result type: {type(current_result).__name__}"
        )

    end_time = time.time_ns()
    duration = (end_time - start_time) / 1_000_000
    logger.trace(f"Run action end '{request.action_name}', duration: {duration}ms")
    return schemas.RunActionResponse(result=result_dict)


async def execute_action_handler(
    action: domain.Action,
    payload: code_action.RunActionPayload | None,
    run_context: code_action.RunActionContext,
    runner_context: context.RunnerContext,
    project_def: domain.Project,
) -> code_action.RunActionResult | None:
    logger.trace(f"Run {action.name} on {str(payload)[:100]}...")
    start_time = time.time_ns()

    if action.name in runner_context.actions_instances_by_name:
        action_instance = runner_context.actions_instances_by_name[action.name]
        action_run_func = action_instance.run
        logger.trace(f"Instance of action {action.name} found in cache")
    else:
        logger.trace(f"Load action {action.name}")
        try:
            action_handler = run_utils.import_module_member_by_source_str(action.source)
        except ModuleNotFoundError as error:
            logger.error(
                f"Source of action {action.name} '{action.source}' could not be imported"
            )
            logger.error(error)
            return

        try:
            # TODO: get config class name from annotation?
            action_config_cls = run_utils.import_module_member_by_source_str(
                action.source + "Config"
            )
        except ModuleNotFoundError as error:
            logger.error(
                f"Source of action config {action.name} '{action.source}Config' could not be imported"
            )
            logger.error(error)
            return

        try:
            action_config = project_def.actions_configs[action.name]
        except KeyError:
            # tmp solution for matching configs like lint and lint_many
            if action.name.endswith("_many"):
                single_action_name = action.name[: -(len("_many"))]
                action_config = project_def.actions_configs.get(single_action_name, {})
            else:
                action_config = {}

        config = action_config_cls(**action_config)
        project_path = project_def.path
        project_cache_dir = project_dirs.get_project_dir(project_path=project_path)
        context = code_action.ActionContext(
            project_dir=project_path, cache_dir=project_cache_dir
        )
        if inspect.isclass(action_handler):
            args = resolve_func_args_with_di(
                func=action_handler.__init__,
                known_args={"config": config, "context": context},
                params_to_ignore=["self"],
            )

            action_instance = action_handler(**args)
            runner_context.actions_instances_by_name[action.name] = action_instance
            action_run_func = action_instance.run
        else:
            action_run_func = action_handler

    args = resolve_func_args_with_di(
        func=action_run_func,
        known_args={"payload": payload, "run_context": run_context},
    )
    # TODO: cache parameters
    try:
        # there is also `inspect.iscoroutinefunction` but it cannot recognize coroutine functions
        # which are class methods.
        call_result = action_run_func(**args)
        if inspect.isawaitable(call_result):
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
        f"End of execution of action {action.name} on {str(payload)[:100]}..., duration: {duration}ms"
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
        available_actions_str = ','.join([action_name for action_name in project_def.actions])
        logger.warning(
            f"Action {action_name} not found. Available actions: {available_actions_str}"
        )
        # TODO: raise error
        return

    actions_to_remove = [action_name, *action_obj.subactions]

    for _action_name in actions_to_remove:
        try:
            del global_state.runner_context.actions_instances_by_name[_action_name]
            logger.trace(f"Removed '{_action_name}' instance from cache")
        except KeyError:
            logger.info(
                f"Tried to reload action '{_action_name}', but it was not found"
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
