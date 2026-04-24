import collections.abc
import functools
import importlib.metadata
import pathlib
import re
from typing import Any, Callable

import ordered_set

from loguru import logger

from finecode_extension_api.interfaces import (  # idevenvinfoprovider,
    icache,
    icommandrunner,
    iextensionrunnerinfoprovider,
    ifileeditor,
    ifilemanager,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
    irepositorycredentialsprovider,
    iworkspaceactionrunner,
)

from finecode_extension_runner import context, domain
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.run_utils import import_module_member_by_source_str
from finecode_extension_runner.impls import (  # dev_env_info_provider,
    command_runner,
    extension_runner_info_provider,
    file_editor,
    file_manager,
    inmemory_cache,
    loguru_logger,
    project_action_runner,
    project_info_provider,
    repository_credentials_provider,
    service_registry,
    workspace_action_runner,
)

def bootstrap(
    registry: Registry,
    runner_context: context.RunnerContext,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[
        [str], collections.abc.Awaitable[dict[str, Any]]
    ],
    current_project_raw_config_version_getter: Callable[[], int],
    cache_dir_path_getter: Callable[[], pathlib.Path],
    actions_getter: Callable[[], dict[str, domain.ActionDeclaration]],
    current_env_name_getter: Callable[[], str],
    handler_packages: set[str],
    service_declarations: list,
    send_request_to_wm: Callable[[str, dict], collections.abc.Awaitable[Any]] | None = None,
):
    # logger_instance = loguru_logger.LoguruLogger()
    logger_instance = loguru_logger.get_logger()

    command_runner_instance = command_runner.CommandRunner(logger=logger_instance)
    # dev_env_info_provider_instance = dev_env_info_provider.DevEnvInfoProvider(logger=logger_instance)
    file_manager_instance = file_manager.FileManager(
        logger=logger_instance,
    )
    file_editor_instance = file_editor.FileEditor(
        logger=logger_instance, file_manager=file_manager_instance
    )
    cache_instance = inmemory_cache.InMemoryCache(
        file_editor=file_editor_instance, logger=logger_instance
    )
    registry.register_instance(ilogger.ILogger, logger_instance)
    registry.register_instance(icommandrunner.ICommandRunner, command_runner_instance)
    registry.register_instance(ifilemanager.IFileManager, file_manager_instance)
    registry.register_instance(ifileeditor.IFileEditor, file_editor_instance)
    registry.register_instance(icache.ICache, cache_instance)
    registry.register_instance(
        iprojectactionrunner.IProjectActionRunner,
        project_action_runner.ProjectActionRunnerImpl(
            send_request_to_wm,
            run_action_func=functools.partial(
                run_action_service.run_action, runner_context=runner_context
            ),
            actions_getter=actions_getter,
            current_env_name_getter=current_env_name_getter,
        ),
    )
    registry.register_instance(
        iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_action_runner.WorkspaceActionRunnerImpl(send_request_to_wm),
    )
    registry.register_instance(
        irepositorycredentialsprovider.IRepositoryCredentialsProvider,
        repository_credentials_provider.ConfigRepositoryCredentialsProvider(),
    )

    # registry.register_instance(idevenvinfoprovider.IDevEnvInfoProvider, dev_env_info_provider_instance)

    registry.register_factory(
        iprojectinfoprovider.IProjectInfoProvider,
        functools.partial(
            project_info_provider_factory,
            project_def_path_getter=project_def_path_getter,
            project_raw_config_getter=project_raw_config_getter,
            current_project_raw_config_version_getter=current_project_raw_config_version_getter,
        ),
    )
    registry.register_factory(
        iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        functools.partial(
            extension_runner_info_provider_factory,
            cache_dir_path_getter=cache_dir_path_getter,
            current_env_name_getter=current_env_name_getter,
        ),
    )

    svc_registry = service_registry.ServiceRegistry(di_registry=registry)
    _activate_extensions(handler_packages, svc_registry)
    _apply_user_service_config(service_declarations, svc_registry)


def _activate_extensions(
    handler_packages: set[str],
    svc_registry: service_registry.ServiceRegistry,
) -> None:
    all_eps = {
        ep.name: ep
        for ep in importlib.metadata.entry_points(group="finecode.activator")
    }
    packages_to_activate = _collect_activatable_packages(handler_packages, all_eps)

    for pkg_name in packages_to_activate:
        try:
            activator_cls = all_eps[pkg_name].load()
            activator_cls(registry=svc_registry).activate()
            logger.trace(f"Activated extension '{pkg_name}'")
        except Exception as e:
            logger.error(f"Failed to activate extension '{pkg_name}': {e}")


def _apply_user_service_config(
    service_declarations: list[object],
    svc_registry: service_registry.ServiceRegistry,
) -> None:
    for svc in service_declarations:
        try:
            interface = import_module_member_by_source_str(svc.interface)
            impl_cls = import_module_member_by_source_str(svc.source)
            svc_registry.register_impl(interface, impl_cls)
            logger.trace(f"Configured service '{svc.source}' for '{svc.interface}'")
        except Exception as e:
            logger.error(f"Failed to configure service '{svc.source}': {e}")


def _collect_activatable_packages(
    seed_packages: set[str],
    all_eps: dict[str, importlib.metadata.EntryPoint],
) -> ordered_set.OrderedSet[str]:
    """Expand seed_packages to include transitive deps that have activators."""
    result: ordered_set.OrderedSet[str] = ordered_set.OrderedSet([])
    visited: set[str] = set()
    queue = list(seed_packages)

    while queue:
        pkg = queue.pop()
        normalized = _normalize_pkg_name(pkg)
        if normalized in visited:
            continue
        visited.add(normalized)

        if normalized in all_eps:
            result.add(normalized)

        try:
            requires = importlib.metadata.requires(pkg) or []
        except importlib.metadata.PackageNotFoundError:
            continue

        for req_str in requires:
            dep_name = _parse_dep_name(req_str)
            dep_normalized = _normalize_pkg_name(dep_name)
            if dep_normalized not in visited and dep_normalized in all_eps:
                queue.append(dep_name)

    return result


def _normalize_pkg_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _parse_dep_name(req_str: str) -> str:
    # PEP 508: package name precedes any version specifier, extra marker, or whitespace
    return re.split(r"[\s>=<!~\(;]", req_str)[0]


def project_info_provider_factory(
    _,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[
        [str], collections.abc.Awaitable[dict[str, Any]]
    ],
    current_project_raw_config_version_getter: Callable[[], int],
):
    return project_info_provider.ProjectInfoProvider(
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter,
    )


async def extension_runner_info_provider_factory(
    registry,
    cache_dir_path_getter: Callable[[], pathlib.Path],
    current_env_name_getter: Callable[[], str],
):
    logger = await registry.get_instance(ilogger.ILogger)
    return extension_runner_info_provider.ExtensionRunnerInfoProvider(
        cache_dir_path_getter=cache_dir_path_getter,
        logger=logger,
        current_env_name_getter=current_env_name_getter,
    )
