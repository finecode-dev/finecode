"""
Requirements on DI in FineCode:
- using interfaces in functions and injecting implementations
- all implementations are singletons
-- but technically it should be possible to add non-singletons
- default container is known
- default container can be customized at one place, no dynamic changes
"""

from typing import Any, Callable, Type, TypeVar

try:
    import fine_python_ast
except ImportError:
    fine_python_ast = None

try:
    import fine_python_mypy
except ImportError:
    fine_python_mypy = None

from finecode.extension_runner import global_state
from finecode.extension_runner.impls import (
    command_runner,
    file_manager,
    inmemory_cache,
    loguru_logger,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icache,
    icommandrunner,
    ifilemanager,
    ilogger,
)

container: dict[str, Any] = {}


def bootstrap(get_document_func: Callable, save_document_func: Callable):
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
    container[ilogger.ILogger] = logger_instance
    container[icommandrunner.ICommandRunner] = command_runner_instance
    container[ifilemanager.IFileManager] = file_manager_instance
    container[icache.ICache] = cache_instance

    # TODO: parameters from config
    ...


T = TypeVar("T")


def get_service_instance(service_type: Type[T]) -> T:
    if service_type == code_action.ActionHandlerLifecycle:
        return code_action.ActionHandlerLifecycle()

    # singletons
    if service_type in container:
        return container[service_type]
    else:
        match service_type:
            case fine_python_ast.IPythonSingleAstProvider:
                service_instance = fine_python_ast.PythonSingleAstProvider(
                    file_manager=container[ifilemanager.IFileManager],
                    cache=container[icache.ICache],
                    logger=container[ilogger.ILogger],
                )
            case fine_python_mypy.IMypySingleAstProvider:
                service_instance = fine_python_mypy.MypySingleAstProvider(
                    file_manager=container[ifilemanager.IFileManager],
                    cache=container[icache.ICache],
                    logger=container[ilogger.ILogger],
                )
            case _:
                raise NotImplementedError(f"No implementation found for {service_type}")

        container[service_type] = service_instance
        return service_instance
