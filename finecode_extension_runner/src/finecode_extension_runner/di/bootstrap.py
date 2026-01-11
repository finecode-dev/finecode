import collections.abc
import functools
import pathlib
from typing import Any, Callable

try:
    import fine_python_ast
except ImportError:
    fine_python_ast = None

try:
    import fine_python_mypy
except ImportError:
    fine_python_mypy = None

try:
    import fine_python_package_info
except ImportError:
    fine_python_package_info = None

from finecode_extension_api.interfaces import (
    iactionrunner,
    icache,
    icommandrunner,
    idevenvinfoprovider,
    ifileeditor,
    ifilemanager,
    ilogger,
    iprojectinfoprovider,
    iextensionrunnerinfoprovider,
    iprojectfileclassifier,
    ipypackagelayoutinfoprovider,
)
from finecode_extension_runner import domain
from finecode_extension_runner._services import run_action
from finecode_extension_runner.di import _state, resolver
from finecode_extension_runner.impls import (
    action_runner,
    command_runner,
    dev_env_info_provider,
    file_editor,
    file_manager,
    inmemory_cache,
    loguru_logger,
    project_info_provider,
    extension_runner_info_provider,
    project_file_classifier,
)


def bootstrap(
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[[str], collections.abc.Awaitable[dict[str, Any]]],
    current_project_raw_config_version_getter: Callable[[], int],
    cache_dir_path_getter: Callable[[], pathlib.Path],
    actions_names_getter: Callable[[], list[str]],
    action_by_name_getter: Callable[[str], domain.Action],
    current_env_name_getter: Callable[[], str]
):
    # logger_instance = loguru_logger.LoguruLogger()
    logger_instance = loguru_logger.get_logger()
    
    command_runner_instance = command_runner.CommandRunner(logger=logger_instance)
    dev_env_info_provider_instance = dev_env_info_provider.DevEnvInfoProvider(logger=logger_instance)
    file_manager_instance = file_manager.FileManager(
        logger=logger_instance,
    )
    file_editor_instance = file_editor.FileEditor(logger=logger_instance, file_manager=file_manager_instance)
    cache_instance = inmemory_cache.InMemoryCache(
        file_editor=file_editor_instance, logger=logger_instance
    )
    action_runner_instance = action_runner.ActionRunner(
        run_action_func=run_action.run_action,
        actions_names_getter=actions_names_getter,
        action_by_name_getter=action_by_name_getter
    )
    _state.container[ilogger.ILogger] = logger_instance
    _state.container[icommandrunner.ICommandRunner] = command_runner_instance
    _state.container[ifilemanager.IFileManager] = file_manager_instance
    _state.container[ifileeditor.IFileEditor] = file_editor_instance
    _state.container[icache.ICache] = cache_instance
    _state.container[iactionrunner.IActionRunner] = action_runner_instance
    _state.container[idevenvinfoprovider.IDevEnvInfoProvider] = dev_env_info_provider_instance

    if fine_python_ast is not None:
        _state.factories[fine_python_ast.IPythonSingleAstProvider] = (
            python_single_ast_provider_factory
        )
    if fine_python_mypy is not None:
        _state.factories[fine_python_mypy.IMypySingleAstProvider] = (
            mypy_single_ast_provider_factory
        )
    _state.factories[iprojectinfoprovider.IProjectInfoProvider] = functools.partial(
        project_info_provider_factory,
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter
    )
    _state.factories[iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider] = (
        functools.partial(
            extension_runner_info_provider_factory,
            cache_dir_path_getter=cache_dir_path_getter,
            current_env_name_getter=current_env_name_getter
        )
    )
    _state.factories[iprojectfileclassifier.IProjectFileClassifier] = (
        project_file_classifier_factory
    )

    if fine_python_package_info is not None:
        _state.factories[ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider] = (
            py_package_layout_info_provider_factory
        )

    # TODO: parameters from config


def python_single_ast_provider_factory(container):
    return fine_python_ast.PythonSingleAstProvider(
        file_editor=container[ifileeditor.IFileEditor],
        cache=container[icache.ICache],
        logger=container[ilogger.ILogger],
    )


def mypy_single_ast_provider_factory(container):
    return fine_python_mypy.MypySingleAstProvider(
        file_editor=container[ifileeditor.IFileEditor],
        cache=container[icache.ICache],
        logger=container[ilogger.ILogger],
    )


def project_info_provider_factory(
    container,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[[str], collections.abc.Awaitable[dict[str, Any]]],
    current_project_raw_config_version_getter: Callable[[], int]
):
    return project_info_provider.ProjectInfoProvider(
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter
    )


async def extension_runner_info_provider_factory(
    container,
    cache_dir_path_getter: Callable[[], pathlib.Path],
    current_env_name_getter: Callable[[], str]
):
    logger = await resolver.get_service_instance(ilogger.ILogger)
    return extension_runner_info_provider.ExtensionRunnerInfoProvider(
        cache_dir_path_getter=cache_dir_path_getter, logger=logger, current_env_name_getter=current_env_name_getter
    )


async def project_file_classifier_factory(
    container,
):
    project_info_provider = await resolver.get_service_instance(
        iprojectinfoprovider.IProjectInfoProvider
    )
    py_package_layout_info_provider = await resolver.get_service_instance(
        ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider
    )
    return project_file_classifier.ProjectFileClassifier(
        project_info_provider=project_info_provider,
        py_package_layout_info_provider=py_package_layout_info_provider,
    )


async def py_package_layout_info_provider_factory(container):
    file_editor = await resolver.get_service_instance(ifileeditor.IFileEditor)
    cache = await resolver.get_service_instance(icache.ICache)
    return fine_python_package_info.PyPackageLayoutInfoProvider(
        file_editor=file_editor, cache=cache
    )
