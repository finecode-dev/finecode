import importlib
import sys
import types
import typing
from pathlib import Path

from loguru import logger

from finecode_extension_runner import context, domain, global_state, schemas
from finecode_extension_runner._services.run_action import (
    ActionFailedException,
    StopWithResponse,
    run_action,
)
from finecode_extension_runner.di import bootstrap as di_bootstrap


async def update_config(
    request: schemas.UpdateConfigRequest,
    document_requester: typing.Callable,
    document_saver: typing.Callable,
    project_raw_config_getter: typing.Callable[
        [str], typing.Awaitable[dict[str, typing.Any]]
    ],
) -> schemas.UpdateConfigResponse:
    project_dir_path = Path(request.working_dir)

    actions: dict[str, domain.Action] = {}
    for action_name, action_schema_obj in request.actions.items():
        handlers: list[domain.ActionHandler] = []
        for handler_obj in action_schema_obj.handlers:
            handlers.append(
                domain.ActionHandler(
                    name=handler_obj.name,
                    source=handler_obj.source,
                    config=handler_obj.config,
                )
            )
        action = domain.Action(
            name=action_name,
            config=action_schema_obj.config,
            handlers=handlers,
            source=action_schema_obj.source,
        )
        actions[action_name] = action

    global_state.runner_context = context.RunnerContext(
        project=domain.Project(
            name=request.project_name,
            dir_path=project_dir_path,
            def_path=request.project_def_path,
            actions=actions,
            action_handler_configs=request.action_handler_configs,
        ),
    )

    # currently update_config is called only once directly after runner start. So we can
    # bootstrap here. Should be changed after adding updating configuration on the fly.
    def project_def_path_getter() -> Path:
        assert global_state.runner_context is not None
        return global_state.runner_context.project.def_path

    def cache_dir_path_getter() -> Path:
        assert global_state.runner_context is not None
        project_dir_path = global_state.runner_context.project.dir_path
        project_cache_dir = (
            project_dir_path / ".venvs" / global_state.env_name / "cache"
        )
        if not project_cache_dir.exists():
            project_cache_dir.mkdir()

        return project_cache_dir

    di_bootstrap.bootstrap(
        get_document_func=document_requester,
        save_document_func=document_saver,
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        cache_dir_path_getter=cache_dir_path_getter,
    )

    return schemas.UpdateConfigResponse()


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

    if action_name in global_state.runner_context.action_cache_by_name:
        action_cache = global_state.runner_context.action_cache_by_name[action_name]

        for handler_name, handler_cache in action_cache.handler_cache_by_name.items():
            if handler_cache.exec_info is not None:
                shutdown_action_handler(
                    action_handler_name=handler_name,
                    exec_info=handler_cache.exec_info,
                )

        del global_state.runner_context.action_cache_by_name[action_name]
        logger.trace(f"Removed '{action_name}' instance from cache")

    try:
        action_obj = project_def.actions[action_name]
    except KeyError:
        logger.warning(f"Definition of action {action_name} not found")
        return

    sources_to_remove = [action_obj.source]
    for handler in action_obj.handlers:
        sources_to_remove.append(handler.source)

    for source_to_remove in sources_to_remove:
        source_package = source_to_remove.split(".")[0]

        loaded_package_modules = dict(
            [
                (key, value)
                for key, value in sys.modules.items()
                if key.startswith(source_package)
                and isinstance(value, types.ModuleType)
            ]
        )

        # delete references to these loaded modules from sys.modules
        for key in loaded_package_modules:
            del sys.modules[key]

        logger.trace(f"Remove modules of package '{source_package}' from cache")


def resolve_package_path(package_name: str) -> str:
    try:
        package_path = importlib.util.find_spec(
            package_name
        ).submodule_search_locations[0]
    except Exception:
        raise ValueError(f"Cannot find package {package_name}")

    return package_path


def document_did_open(document_uri: str) -> None:
    if global_state.runner_context is not None:
        global_state.runner_context.docs_owned_by_client.append(document_uri)


def document_did_close(document_uri: str) -> None:
    if global_state.runner_context is not None:
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
    for action_cache in global_state.runner_context.action_cache_by_name.values():
        for handler_name, handler_cache in action_cache.handler_cache_by_name.items():
            if handler_cache.exec_info is not None:
                shutdown_action_handler(
                    action_handler_name=handler_name, exec_info=handler_cache.exec_info
                )


def exit_action_handler(
    action_handler_name: str, exec_info: domain.ActionHandlerExecInfo
) -> None:
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
    for action_cache in global_state.runner_context.action_cache_by_name.values():
        for handler_name, handler_cache in action_cache.handler_cache_by_name.items():
            if handler_cache.exec_info is not None:
                exec_info = handler_cache.exec_info
                exit_action_handler(
                    action_handler_name=handler_name, exec_info=exec_info
                )
        action_cache.handler_cache_by_name = {}
