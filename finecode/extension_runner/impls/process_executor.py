import asyncio
import concurrent.futures
import contextlib
import functools
import multiprocessing as mp

from loguru import logger

from finecode_extension_api.interfaces import iprocessexecutor


class ProcessExecutor(iprocessexecutor.IProcessExecutor):
    def __init__(self) -> None:
        self._py_process_executor: concurrent.futures.ProcessPoolExecutor | None = None
        self._active: bool = False

    @contextlib.contextmanager
    def activate(self) -> None:
        self._active = True
        try:
            yield
        except Exception as exc:
            logger.exception(exc)
            raise exc
        finally:
            if self._py_process_executor is not None:
                self._py_process_executor.shutdown()
                self._py_process_executor = None

    async def submit(self, func, *args, **kwargs):
        if not self._active:
            raise Exception("Process Executor is not activated")

        if self._py_process_executor is None:
            # TODO: only for POSIX
            # forkserver is default in Python 3.14+, use the same also with older
            # versions
            mp_context = mp.get_context("forkserver")
            self._py_process_executor = concurrent.futures.ProcessPoolExecutor(
                mp_context=mp_context
            )

        loop = asyncio.get_running_loop()
        func_to_execute = func
        if len(kwargs) > 0:
            func_to_execute = functools.partial(func, **kwargs)

        logger.debug(
            f"Run in process executor, queue: {self._py_process_executor._queue_count}, processes: {len(self._py_process_executor._processes)}, max workers: {self._py_process_executor._max_workers}"
        )
        try:
            result = await loop.run_in_executor(
                self._py_process_executor, func_to_execute, *args
            )
        except Exception as exc:
            logger.exception(exc)
            raise exc
        return result
