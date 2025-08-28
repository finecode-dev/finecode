import functools
import pathlib
from typing import Any, Awaitable, Callable, Type, TypeVar

try:
    import fine_python_ast
except ImportError:
    fine_python_ast = None

try:
    import fine_python_mypy
except ImportError:
    fine_python_mypy = None

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    icache,
    icommandrunner,
    ifilemanager,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_runner import global_state, schemas
from finecode_extension_runner._services import run_action
from finecode_extension_runner.di import _state
from finecode_extension_runner.impls import (
    action_runner,
    command_runner,
    file_manager,
    inmemory_cache,
    loguru_logger,
    project_info_provider,
)


def bootstrap(
    get_document_func: Callable,
    save_document_func: Callable,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[[str], Awaitable[dict[str, Any]]],
):
    # logger_instance = loguru_logger.LoguruLogger()
    logger_instance = loguru_logger.get_logger()
    command_runner_instance = command_runner.CommandRunner(logger=logger_instance)
    file_manager_instance = file_manager.FileManager(
        docs_owned_by_client=global_state.runner_context.docs_owned_by_client,
        get_document_func=get_document_func,
        save_document_func=save_document_func,
        logger=logger_instance,
    )
    cache_instance = inmemory_cache.InMemoryCache(
        file_manager=file_manager_instance, logger=logger_instance
    )
    action_runner_instance = action_runner.ActionRunner(
        internal_service_func=run_action_wrapper
    )
    _state.container[ilogger.ILogger] = logger_instance
    _state.container[icommandrunner.ICommandRunner] = command_runner_instance
    _state.container[ifilemanager.IFileManager] = file_manager_instance
    _state.container[icache.ICache] = cache_instance
    _state.container[iactionrunner.IActionRunner] = action_runner_instance

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
    )

    # TODO: parameters from config


async def run_action_wrapper(
    action_name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    request = schemas.RunActionRequest(action_name=action_name, params=payload)
    options = schemas.RunActionOptions(result_format="json")

    try:
        response = await run_action.run_action(request=request, options=options)
    except run_action.ActionFailedException as exception:
        raise iactionrunner.ActionRunFailed(exception.message)

    return response.result


def python_single_ast_provider_factory(container):
    return fine_python_ast.PythonSingleAstProvider(
        file_manager=container[ifilemanager.IFileManager],
        cache=container[icache.ICache],
        logger=container[ilogger.ILogger],
    )


def mypy_single_ast_provider_factory(container):
    return fine_python_mypy.MypySingleAstProvider(
        file_manager=container[ifilemanager.IFileManager],
        cache=container[icache.ICache],
        logger=container[ilogger.ILogger],
    )


def project_info_provider_factory(
    container,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[[str], Awaitable[dict[str, Any]]],
):
    return project_info_provider.ProjectInfoProvider(
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
    )
