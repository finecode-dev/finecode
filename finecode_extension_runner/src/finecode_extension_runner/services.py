import json
import collections.abc
import hashlib
import importlib
import sys
import types
import typing
from pathlib import Path

from loguru import logger
from finecode_extension_api import service

from finecode_extension_runner import context, domain, global_state, schemas, run_utils, schema_utils
from finecode_extension_runner._services.run_action import (
    ActionFailedException,
    StopWithResponse,
    run_action_raw,
    create_action_exec_info,
    ensure_handler_instantiated,
)
from finecode_extension_runner.di import bootstrap as di_bootstrap


def _compute_request_hash(request: schemas.UpdateConfigRequest) -> int:
    """Compute a hash of the request object for version tracking."""
    request_dict = request.to_dict()
    # Convert Path objects to strings for JSON serialization
    request_dict["working_dir"] = str(request_dict["working_dir"])
    request_dict["project_def_path"] = str(request_dict["project_def_path"])

    # Sort keys for consistent hashing
    request_json = json.dumps(request_dict, sort_keys=True)
    hash_bytes = hashlib.sha256(request_json.encode()).digest()
    # Convert first 8 bytes to integer for version number
    return int.from_bytes(hash_bytes[:8], byteorder="big")


async def update_config(
    request: schemas.UpdateConfigRequest,
    project_raw_config_getter: typing.Callable[
        [str], collections.abc.Awaitable[dict[str, typing.Any]]
    ],
    send_request_to_wm: typing.Callable[[str, dict], collections.abc.Awaitable[typing.Any]] | None = None,
) -> schemas.UpdateConfigResponse:
    project_dir_path = Path(request.working_dir)

    actions: dict[str, domain.ActionDeclaration] = {}
    for action_name, action_schema_obj in request.actions.items():
        handlers: list[domain.ActionHandlerDeclaration] = []
        for handler_obj in action_schema_obj.handlers:
            handlers.append(
                domain.ActionHandlerDeclaration(
                    name=handler_obj.name,
                    source=handler_obj.source,
                    config=handler_obj.config,
                )
            )
        action = domain.ActionDeclaration(
            name=action_name,
            config=action_schema_obj.config,
            handlers=handlers,
            source=action_schema_obj.source,
        )
        if action_schema_obj.source is not None:
            duplicate = next(
                (a for a in actions.values() if a.source == action.source),
                None,
            )
            if duplicate is not None:
                raise ValueError(
                    f"Action source '{action.source}' is already registered as "
                    f"'{duplicate.name}'. Each action class may only be registered "
                    f"once (ADR-0007)."
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
    global_state.runner_context.project_config_version = _compute_request_hash(request)

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

    def current_project_raw_config_version_getter() -> int:
        return global_state.runner_context.project_config_version

    def actions_getter() -> dict[str, domain.ActionDeclaration]:
        assert global_state.runner_context is not None
        return global_state.runner_context.project.actions

    def current_env_name_getter() -> str:
        return global_state.env_name

    handler_packages = {
        handler.source.split(".")[0]
        for action in actions.values()
        for handler in action.handlers
    } | {
        svc.source.split(".")[0] for svc in request.services
    }

    di_bootstrap.bootstrap(
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        cache_dir_path_getter=cache_dir_path_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter,
        actions_getter=actions_getter,
        current_env_name_getter=current_env_name_getter,
        handler_packages=handler_packages,
        service_declarations=request.services,
        send_request_to_wm=send_request_to_wm,
    )

    if request.handlers_to_initialize is not None:
        await initialize_handlers(request.handlers_to_initialize)

    return schemas.UpdateConfigResponse()


async def resolve_action_sources() -> dict[str, str]:
    """Resolve canonical (fully qualified) sources for all known actions.

    The config source may be a re-exported import path (e.g.
    ``finecode_extension_api.LintAction``) while callers that hold a class object
    always derive the source from ``__module__.__qualname__``, producing the
    canonical path (e.g. ``finecode_extension_api.actions.lint.LintAction``).
    Returns a mapping of config source → canonical source for entries that differ.
    """
    if global_state.runner_context is None:
        return {}
    actions = global_state.runner_context.project.actions
    resolved: dict[str, str] = {}
    for action in actions.values():
        if action.source is None:
            continue
        try:
            cls = run_utils.import_module_member_by_source_str(action.source)
            canonical = f"{cls.__module__}.{cls.__qualname__}"
            if canonical != action.source:
                resolved[action.source] = canonical
        except Exception as exception:
            logger.trace(f'Failed to import action {action.source}: {exception}')
    return resolved


async def initialize_handlers(
    handlers_by_action: dict[str, list[str]],
) -> None:
    """Eagerly instantiate and initialize handlers.

    This is called after update_config to pre-instantiate handlers so that
    services (like LSP services) are started early rather than on first use.

    Args:
        handlers_by_action: mapping of action name → list of handler names
            to eagerly initialize.
    """
    if global_state.runner_context is None:
        logger.warning("Cannot initialize handlers: runner context is not set")
        return

    runner_context = global_state.runner_context
    project = runner_context.project

    for action_name, handler_names in handlers_by_action.items():
        action_def = project.actions.get(action_name)
        if action_def is None:
            logger.warning(
                f"Action '{action_name}' not found, skipping handler initialization"
            )
            continue

        if action_name in runner_context.action_cache_by_name:
            action_cache = runner_context.action_cache_by_name[action_name]
        else:
            action_cache = domain.ActionCache()
            runner_context.action_cache_by_name[action_name] = action_cache

        if action_cache.exec_info is None:
            action_cache.exec_info = create_action_exec_info(action_def)

        handlers_to_init = [
            h for h in action_def.handlers if h.name in handler_names
        ]
        for handler in handlers_to_init:
            if handler.name in action_cache.handler_cache_by_name:
                handler_cache = action_cache.handler_cache_by_name[handler.name]
                if handler_cache.instance is not None:
                    continue
            else:
                handler_cache = domain.ActionHandlerCache()
                action_cache.handler_cache_by_name[handler.name] = handler_cache

            try:
                await ensure_handler_instantiated(
                    handler=handler,
                    handler_cache=handler_cache,
                    action_exec_info=action_cache.exec_info,
                    runner_context=runner_context,
                )
                logger.trace(
                    f"Eagerly initialized handler '{handler.name}' "
                    f"for action '{action_name}'"
                )
            except Exception as e:
                logger.error(
                    f"Failed to eagerly initialize handler '{handler.name}' "
                    f"for action '{action_name}': {e}"
                )


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
                    handler_instance=handler_cache.instance,
                    exec_info=handler_cache.exec_info,
                    used_services=handler_cache.used_services,
                    runner_context=global_state.runner_context,
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
    except Exception as exception:
        raise ValueError(f"Cannot find package {package_name}") from exception

    return package_path


def shutdown_action_handler(
    action_handler_name: str,
    handler_instance: domain.ActionHandlerDeclaration | None,
    exec_info: domain.ActionHandlerExecInfo,
    used_services: list[service.Service],
    runner_context: context.RunnerContext,
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

    if handler_instance is not None:
        for used_service in used_services:
            running_service_info = runner_context.running_services[used_service]
            running_service_info.used_by.remove(handler_instance)
            if len(running_service_info.used_by) == 0:
                if isinstance(used_service, service.DisposableService):
                    try:
                        used_service.dispose()
                        logger.trace(f"Disposed service: {used_service}")
                    except Exception as exception:
                        logger.error(f"Failed to dispose service: {used_service}")
                        logger.exception(exception)


def shutdown_all_action_handlers() -> None:
    if global_state.runner_context is not None:
        logger.trace("Shutdown all action handlers")
        for action_cache in global_state.runner_context.action_cache_by_name.values():
            for (
                handler_name,
                handler_cache,
            ) in action_cache.handler_cache_by_name.items():
                if handler_cache.exec_info is not None:
                    shutdown_action_handler(
                        action_handler_name=handler_name,
                        handler_instance=handler_cache.instance,
                        exec_info=handler_cache.exec_info,
                        used_services=handler_cache.used_services,
                        runner_context=global_state.runner_context,
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
    if global_state.runner_context is not None:
        logger.trace("Exit all action handlers")
        for action_cache in global_state.runner_context.action_cache_by_name.values():
            for (
                handler_name,
                handler_cache,
            ) in action_cache.handler_cache_by_name.items():
                if handler_cache.exec_info is not None:
                    exec_info = handler_cache.exec_info
                    exit_action_handler(
                        action_handler_name=handler_name, exec_info=exec_info
                    )
            action_cache.handler_cache_by_name = {}


def get_payload_schemas() -> dict[str, dict | None]:
    """Return a payload schema for every action currently known to the runner.

    Called by the WM via the ``actions/getPayloadSchemas`` command to populate
    the schema cache used when building MCP tool descriptions.

    Returns a mapping of action name → JSON Schema fragment (or ``None`` if the
    action class could not be imported or has no ``PAYLOAD_TYPE``).
    """
    if global_state.runner_context is None:
        return {}

    result: dict[str, dict | None] = {}
    for action_name, action in global_state.runner_context.project.actions.items():
        try:
            action_cls = run_utils.import_module_member_by_source_str(action.source)
            payload_cls = getattr(action_cls, "PAYLOAD_TYPE", None)
            if payload_cls is None:
                result[action_name] = None
            else:
                schema = schema_utils.extract_payload_schema(payload_cls)
                doc = getattr(action_cls, "__doc__", None)
                if doc:
                    schema["description"] = doc.strip()
                result[action_name] = schema
        except Exception as exception:
            logger.debug(f"Could not extract payload schema for action '{action_name}': {exception}")
            result[action_name] = None

    return result
