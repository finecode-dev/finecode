import collections.abc
import functools
import importlib.metadata
import pathlib
import re
from typing import Any, Callable

import ordered_set

# TODO: get rid of these two tries
try:
    import finecode_httpclient
except ImportError:
    finecode_httpclient = None

try:
    from finecode_jsonrpc import jsonrpc_client
except ImportError:
    jsonrpc_client = None

from loguru import logger

from finecode_extension_api.interfaces import (  # idevenvinfoprovider,
    iactionrunner,
    icache,
    icommandrunner,
    iextensionrunnerinfoprovider,
    ifileeditor,
    ifilemanager,
    ihttpclient,
    ijsonrpcclient,
    ilogger,
    ilspclient,
    iprojectinfoprovider,
    irepositorycredentialsprovider,
)

from finecode_extension_runner import domain
from finecode_extension_runner._services import run_action
from finecode_extension_runner.di import _state, resolver
from finecode_extension_runner.impls import (  # dev_env_info_provider,
    action_runner,
    command_runner,
    extension_runner_info_provider,
    file_editor,
    file_manager,
    inmemory_cache,
    loguru_logger,
    lsp_client,
    project_info_provider,
    repository_credentials_provider,
    service_registry,
)


def bootstrap(
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[
        [str], collections.abc.Awaitable[dict[str, Any]]
    ],
    current_project_raw_config_version_getter: Callable[[], int],
    cache_dir_path_getter: Callable[[], pathlib.Path],
    actions_names_getter: Callable[[], list[str]],
    action_by_name_getter: Callable[[str], domain.ActionDeclaration],
    current_env_name_getter: Callable[[], str],
    handler_packages: set[str],
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
    action_runner_instance = action_runner.ActionRunner(
        run_action_func=run_action.run_action,
        actions_names_getter=actions_names_getter,
        action_by_name_getter=action_by_name_getter,
    )
    _state.container[ilogger.ILogger] = logger_instance
    _state.container[icommandrunner.ICommandRunner] = command_runner_instance
    _state.container[ifilemanager.IFileManager] = file_manager_instance
    _state.container[ifileeditor.IFileEditor] = file_editor_instance
    _state.container[icache.ICache] = cache_instance
    _state.container[iactionrunner.IActionRunner] = action_runner_instance

    if finecode_httpclient is not None:
        _state.container[ihttpclient.IHttpClient] = finecode_httpclient.HttpClient(
            logger=logger_instance
        )

    _state.container[irepositorycredentialsprovider.IRepositoryCredentialsProvider] = (
        repository_credentials_provider.ConfigRepositoryCredentialsProvider()
    )

    if jsonrpc_client is not None:
        json_rpc_client_instance = jsonrpc_client.JsonRpcClientImpl()
        _state.container[ijsonrpcclient.IJsonRpcClient] = json_rpc_client_instance
        _state.container[ilspclient.ILspClient] = lsp_client.LspClientImpl(
            json_rpc_client=json_rpc_client_instance,
        )

    # _state.container[idevenvinfoprovider.IDevEnvInfoProvider] = dev_env_info_provider_instance

    _state.factories[iprojectinfoprovider.IProjectInfoProvider] = functools.partial(
        project_info_provider_factory,
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter,
    )
    _state.factories[iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider] = (
        functools.partial(
            extension_runner_info_provider_factory,
            cache_dir_path_getter=cache_dir_path_getter,
            current_env_name_getter=current_env_name_getter,
        )
    )

    _activate_extensions(handler_packages)


def _activate_extensions(handler_packages: set[str]) -> None:
    registry = service_registry.ServiceRegistry()
    all_eps = {
        ep.name: ep
        for ep in importlib.metadata.entry_points(group="finecode.activator")
    }
    packages_to_activate = _collect_activatable_packages(handler_packages, all_eps)

    for pkg_name in packages_to_activate:
        try:
            activator_cls = all_eps[pkg_name].load()
            activator_cls(registry=registry).activate()
            logger.trace(f"Activated extension '{pkg_name}'")
        except Exception as e:
            logger.error(f"Failed to activate extension '{pkg_name}': {e}")


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
    container,
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
    container,
    cache_dir_path_getter: Callable[[], pathlib.Path],
    current_env_name_getter: Callable[[], str],
):
    logger = await resolver.get_service_instance(ilogger.ILogger)
    return extension_runner_info_provider.ExtensionRunnerInfoProvider(
        cache_dir_path_getter=cache_dir_path_getter,
        logger=logger,
        current_env_name_getter=current_env_name_getter,
    )
