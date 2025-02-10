import importlib
import inspect
import sys
import types
from pathlib import Path
from typing import Any, Callable, Type

from loguru import logger

from finecode.extension_runner import (code_action, context, domain,
                                       global_state, project_dirs, run_utils,
                                       schemas, bootstrap)


class ActionFailedException(Exception): ...


document_requester: Callable


async def get_document(uri: str):
    doc = await document_requester(uri)
    return doc


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
    bootstrap.bootstrap(get_document)

    return schemas.UpdateConfigResponse()


def get_action_payload_type(
    action: domain.Action, all_actions: dict[str, domain.Action]
) -> Type[code_action.RunPayloadType]:
    if action.source is not None:
        try:
            action_handler = run_utils.import_module_member_by_source_str(action.source)
        except ModuleNotFoundError as error:
            logger.error(f"Source of action {action.name} '{action.source}' could not be imported")
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
    else:
        # all subactions should have the same payload type
        if len(action.subactions) == 0:
            raise ValueError(f"Action {action.name} doesn't have neither source nor subactions")

        first_subaction_name = action.subactions[0]
        try:
            first_subaction = all_actions[first_subaction_name]
        except KeyError:
            raise ValueError(f"Subaction {first_subaction_name} not found in actions")

        return get_action_payload_type(first_subaction, all_actions)


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists
    if global_state.runner_context is None:
        # TODO: raise error
        return schemas.RunActionResponse({})

    project = global_state.runner_context.project

    try:
        action_obj = project.actions[request.action_name]
    except KeyError:
        logger.warning(
            f"Action {request.action_name} not found. Available actions: {','.join([action_name for action_name in project.actions])}"
        )
        # TODO: raise error
        return schemas.RunActionResponse({})

    # TODO: cache
    payload_type = get_action_payload_type(action_obj, project.actions)
    # TODO: handle errors
    payload = payload_type(**request.params)
    try:
        result = await __run_action(
            action=action_obj,
            payload=payload,
            project_root=global_state.runner_context.project.path,
            runner_context=global_state.runner_context,
        )
    except Exception as e:
        logger.exception(e)
        raise ActionFailedException("Failed to run action")

    result_dict = {}
    if isinstance(result, code_action.RunActionResult):
        result_dict = result.model_dump()
    else:
        raise ActionFailedException(f"Unexpected result type: {type(result).__name__}")

    return schemas.RunActionResponse(result=result_dict)


async def __run_action(
    action: domain.Action,
    payload: code_action.RunActionPayload,
    project_root: Path,
    runner_context: context.RunnerContext,
) -> code_action.RunActionResult | None:
    logger.trace(f"Execute action {action.name}: {payload}")

    if global_state.runner_context is None:
        # TODO: raise error
        return

    project_def = global_state.runner_context.project
    current_payload: code_action.RunActionPayload = payload
    current_result: code_action.RunActionResult | None = None
    # run in current env
    if len(action.subactions) > 0:
        # TODO: handle circular deps
        for subaction in action.subactions:
            try:
                subaction_obj = runner_context.project.actions[subaction]
            except KeyError:
                raise ValueError(f"Action {subaction} not found")

            subaction_result = await __run_action(
                subaction_obj,
                current_payload,
                project_root=project_root,
                runner_context=runner_context,
            )
            if current_result is None:
                current_result = subaction_result
            else:
                try:
                    current_result.update(subaction_result)
                except NotImplementedError:
                    ...

            if subaction_result is not None:
                try:
                    current_payload = current_result.to_next_payload(current_payload)
                except NotImplementedError:
                    ...
    elif action.source is not None:
        logger.debug(f"Run {action.name} on {current_payload}")

        action_handler_is_class: bool = False
        if action.name in runner_context.actions_instances_by_name:
            action_instance = runner_context.actions_instances_by_name[action.name]
            action_run_func = action_instance.run
            action_handler_is_class = True
            logger.trace(f"Instance of action {action.name} found in cache")
        else:
            logger.trace(f"Load action {action.name}")
            try:
                action_handler = run_utils.import_module_member_by_source_str(action.source)
                # TODO: get config class name from annotation?
                action_config_cls = run_utils.import_module_member_by_source_str(
                    action.source + "Config"
                )
            except ModuleNotFoundError as error:
                logger.error(
                    f"Source of action {action.name} '{action.source}' could not be imported"
                )
                logger.error(error)
                return

            try:
                action_config = project_def.actions_configs[action.name]
            except KeyError:
                action_config = {}

            config = action_config_cls(**action_config)
            project_path = runner_context.project.path
            project_cache_dir = project_dirs.get_project_dir(project_path=project_path)
            context = code_action.ActionContext(
                project_dir=runner_context.project.path, cache_dir=project_cache_dir
            )
            if inspect.isclass(action_handler):
                action_func_parameters = inspect.signature(action_handler.__init__).parameters
                action_func_annotations = inspect.get_annotations(action_handler.__init__, eval_str=True)
                args: dict[str, Any] = {
                    "config": config,
                    "context": context
                }
                for param_name in action_func_parameters.keys():
                    if param_name in ['self', 'config', 'context']:
                        continue
                    # TODO: handle errors
                    param_type = action_func_annotations[param_name]
                    param_value = bootstrap.get_service_instance(param_type)
                    args[param_name] = param_value

                action_instance = action_handler(**args)
                runner_context.actions_instances_by_name[action.name] = action_instance
                action_run_func = action_instance.run
                action_handler_is_class = True
            else:
                action_run_func = action_handler

        logger.debug("Run on single")
        action_func_parameters = inspect.signature(action_run_func).parameters
        action_func_annotations = inspect.get_annotations(action_run_func, eval_str=True)
        args: dict[str, Any] = {
            "payload": payload
        }
        for param_name in action_func_parameters.keys():
            if param_name == 'payload':
                continue
            # TODO: handle errors
            param_type = action_func_annotations[param_name]
            param_value = bootstrap.get_service_instance(param_type)
            args[param_name] = param_value

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
    else:
        logger.warning(f"Action {action.name} has neither source nor subactions, skip it")
        return

    logger.trace(f"End of execution of action {action.name} on {payload}")
    return current_result


def reload_action(action_name: str) -> None:
    if global_state.runner_context is None:
        # TODO: raise error
        return

    project_def = global_state.runner_context.project

    try:
        action_obj = project_def.actions[action_name]
    except KeyError:
        logger.warning(
            f"Action {action_name} not found. Available actions: {','.join([action_name for action_name in project_def.actions])}"
        )
        # TODO: raise error
        return

    actions_to_remove = [action_name, *action_obj.subactions]

    for _action_name in actions_to_remove:
        try:
            del global_state.runner_context.actions_instances_by_name[_action_name]
            logger.trace(f"Removed '{_action_name}' instance from cache")
        except KeyError:
            logger.info(f"Tried to reload action '{_action_name}', but it was not found")

        try:
            action_obj = project_def.actions[action_name]
        except KeyError:
            logger.warning(f"Definition of action {action_name} not found")
            continue

        action_source = action_obj.source
        if action_source is None:
            continue
        action_package = action_source.split('.')[0]

        loaded_package_modules = dict(
            [
                (key, value)
                for key, value in sys.modules.items()
                if key.startswith(action_package) and isinstance(value, types.ModuleType)
            ]
        )

        # delete references to these loaded modules from sys.modules
        for key in loaded_package_modules:
            del sys.modules[key]

        logger.trace(f"Remove modules of package '{action_package}' from cache")


def resolve_package_path(package_name: str) -> str:
    try:
        package_path = importlib.util.find_spec(package_name).submodule_search_locations[0]
    except Exception:
        raise ValueError(f"Cannot find package {package_name}")

    return package_path


def document_did_open(document_uri: str) -> None:
    global_state.runner_context.docs_owned_by_client.append(document_uri)


def document_did_close(document_uri: str) -> None:
    global_state.runner_context.docs_owned_by_client.remove(document_uri)
