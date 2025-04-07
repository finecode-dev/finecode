import asyncio
import concurrent.futures

from finecode_extension_api.interfaces import iprocessexecutor

class ProcessExecutor(iprocessexecutor.IProcessExecutor):
    def __init__(self) -> None:
        self._py_process_executor: concurrent.futures.ProcessPoolExecutor | None = None
    
    def start(self) -> None:
        # TODO: make available only for ER internals
        self._py_process_executor = concurrent.futures.ProcessPoolExecutor()

    def stop(self) -> None:
        # TODO: make available only for ER internals
        self._py_process_executor.shutdown()

    def is_running(self) -> None:
        # TODO: make available only for ER internals
        return self._py_process_executor is not None

    async def submit(self, func, *args):
        if self._py_process_executor is None:
            raise Exception("Process executor can be used only in action handler run function")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self.py_process_executor, func, *args)
        return result
