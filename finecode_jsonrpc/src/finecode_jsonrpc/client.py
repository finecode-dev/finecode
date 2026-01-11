from __future__ import annotations

import traceback

import enum
import dataclasses
import functools
import os
import subprocess
import sys
from pathlib import Path
import asyncio
import json
import re
import threading
import typing
import uuid
import concurrent.futures
import collections.abc

import culsans
import apischema
from finecode_jsonrpc import _io_thread
from loguru import logger


class QueueEnd:
    # just object() would not support multiprocessing, use class and compare by it
    @typing.override
    def __eq__(self, other: object) -> bool:
        return self.__class__ == other.__class__


QUEUE_END = QueueEnd()


# JSON-RPC 2.0 Standard Error Codes
# See: https://www.jsonrpc.org/specification#error_object
class JsonRpcErrorCode:
    """Standard JSON-RPC 2.0 error codes."""

    PARSE_ERROR = -32700
    """Invalid JSON was received by the server. An error occurred on the server while parsing the JSON text."""

    INVALID_REQUEST = -32600
    """The JSON sent is not a valid Request object."""

    METHOD_NOT_FOUND = -32601
    """The method does not exist / is not available."""

    INVALID_PARAMS = -32602
    """Invalid method parameter(s)."""

    INTERNAL_ERROR = -32603
    """Internal JSON-RPC error."""

    # -32000 to -32099: Server error - Reserved for implementation-defined server-errors


class WriterFromQueue:
    def __init__(self, out_queue: culsans.SyncQueue[bytes]) -> None:
        self._out_queue: typing.Final = out_queue

    def close(self) -> None:
        self._out_queue.put(QUEUE_END)

    def write(self, data: bytes) -> None:
        self._out_queue.put(data)


class RunnerFailedToStart(Exception):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message: typing.Final = message


class BaseRunnerRequestException(Exception):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message: typing.Final = message


class NoResponse(BaseRunnerRequestException): ...


class InvalidResponse(BaseRunnerRequestException): ...


class ResponseTimeout(BaseRunnerRequestException): ...


@dataclasses.dataclass
class ResponseError:
    """https://www.jsonrpc.org/specification#error_object"""

    code: int
    """A number indicating the error type that occurred."""
    message: str
    """A string providing a short description of the error."""
    data: typing.Any | None = None
    """A primitive or structured value that contains additional information
    about the error. Can be omitted."""


class ErrorOnRequest(BaseRunnerRequestException):
    def __init__(self, error: ResponseError) -> None:
        super().__init__(message=f"Got error {error.code}: {error.message}")
        self.error = error


class CommunicationType(enum.Enum):
    TCP = enum.auto()
    STDIO = enum.auto()


def task_done_log_callback(future: asyncio.Future[typing.Any], task_id: str = ""):
    if future.cancelled():
        logger.debug(f"task cancelled: {task_id}")
    else:
        exc = future.exception()
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error(
                f"exception in task: {task_id} {type(exc)} {exc.message if hasattr(exc, 'message') else ''}"
            )
            logger.exception(exc)
            logger.error(tb)
        else:
            logger.trace(f"{task_id} done")


class RequestCancelledError(asyncio.CancelledError):
    def __init__(self, request_id: int | str) -> None:
        super().__init__()
        self.request_id = request_id


class JsonRpcClient:
    CHARSET: typing.Final[str] = "utf-8"
    CONTENT_TYPE: typing.Final[str] = "application/vscode-jsonrpc"
    VERSION: typing.Final[str] = "2.0"

    def __init__(self, message_types: dict[str, typing.Any], readable_id: str, communication_type: CommunicationType = CommunicationType.TCP) -> None:
        self.server_process_stopped: typing.Final = threading.Event()
        self.server_exit_callback: (
            collections.abc.Callable[[], collections.abc.Coroutine] | None
        ) = None
        self.in_message_queue: typing.Final = culsans.Queue()
        self.out_message_queue: typing.Final = culsans.Queue()
        self.writer = WriterFromQueue(out_queue=self.out_message_queue.sync_q)
        self.message_types = message_types
        self.readable_id: str = readable_id
        self.communication_type = communication_type

        self._async_tasks: list[asyncio.Task[typing.Any]] = []
        self._stop_event: typing.Final = threading.Event()
        self._sync_request_futures: dict[str, concurrent.futures.Future] = {}
        self._async_request_futures: dict[str, asyncio.Future] = {}
        self._expected_result_type_by_msg_id: dict[str, typing.Any] = {}

        self.feature_impls: dict[str, collections.abc.Callable] = {}
        
        # NOTE: reader and writer can be accessed only in IO thread
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._async_tasks_in_io_thread: list[asyncio.Task[typing.Any]] = []
        self._tcp_port_future: asyncio.Future[int] | None = None

    def feature(self, name: str, impl: collections.abc.Callable) -> None:
        self.feature_impls[name] = impl

    async def start(
        self,
        server_cmd: str,
        working_dir_path: Path,
        io_thread: _io_thread.AsyncIOThread,
        debug_port_future: concurrent.futures.Future[int] | None,
        connect: bool = True
    ) -> None:
        old_working_dir = os.getcwd()
        os.chdir(working_dir_path)

        # temporary remove VIRTUAL_ENV env variable to avoid starting in wrong venv
        old_virtual_env_var = os.environ.pop("VIRTUAL_ENV", None)

        try:
            await self._start_server(full_cmd=server_cmd, io_thread=io_thread, debug_port_future=debug_port_future)
            if connect:
                await self.connect_to_server(io_thread=io_thread)
        finally:
            if old_virtual_env_var is not None:
                os.environ["VIRTUAL_ENV"] = old_virtual_env_var

            os.chdir(old_working_dir)  # restore original working directory

    async def _start_server(self, full_cmd: str, io_thread: _io_thread.AsyncIOThread, debug_port_future: concurrent.futures.Future[int] | None) -> None:
        server_future = io_thread.run_coroutine(
            start_server(
                cmd=full_cmd,
                communication_type=self.communication_type,
                out_message_queue=self.out_message_queue,
                stop_event=self._stop_event,
                server_stopped_event=self.server_process_stopped,
                server_id=self.readable_id,
                async_tasks=self._async_tasks_in_io_thread,
                debug_port_future=debug_port_future
            )
        )

        # add done callback to catch exceptions if coroutine fails
        server_future.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"server_future|{self.readable_id}"
            )
        )

        await asyncio.wrap_future(server_future)
        server_start_exception = server_future.exception()
        if server_start_exception is not None:
            # there are no active tasks yet, no need to stop, just interrupt starting
            # the server
            raise server_start_exception
    
        self._reader, self._writer, self._tcp_port_future = server_future.result()
        
        notify_exit = asyncio.create_task(self._server_process_stop_handler())
        notify_exit.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"notify_exit|{self.readable_id}"
            )
        )

        self._async_tasks.extend([notify_exit])
        logger.debug(f"End of start for {full_cmd}")

    async def connect_to_server(self, io_thread: _io_thread.AsyncIOThread, timeout: float | None = 30):
        connect_to_server_future = io_thread.run_coroutine(
            self._connect_to_server_io(timeout=timeout)
        )

        # add done callback to catch exceptions if coroutine fails
        connect_to_server_future.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"connect_to_server_future|{self.readable_id}"
            )
        )

        await asyncio.wrap_future(connect_to_server_future)
        connect_to_server_future_exception = connect_to_server_future.exception()
        if connect_to_server_future_exception is not None:
            raise connect_to_server_future_exception

        # message processor task ends automatically after getting QUEUE_END message,
        # no need to save it in `_async_tasks` for explicit stop.
        message_processor_task = asyncio.create_task(self.process_incoming_messages())
        message_processor_task.add_done_callback(
            functools.partial(
                task_done_log_callback,
                task_id=f"process_incoming_messages|{self.readable_id}",
            )
        )

    async def _server_process_stop_handler(self):
        """Cleanup handler that runs when the server process managed by the client exits"""
        # await asyncio.to_thread(self.server_process_stopped.wait)

        logger.trace(f"Server process stopped handler {self.readable_id}")
        while not self.server_process_stopped.is_set():
            await asyncio.sleep(0.1)

        logger.debug(f"Server process {self.readable_id} stopped")

        # Cancel any pending requests
        for id_, fut in list(self._sync_request_futures.items()) + list(
            self._async_request_futures.items()
        ):
            if not fut.done():
                fut.set_exception(
                    RuntimeError("Server was stopped before getting the response")
                )
                logger.debug(
                    f"Cancelled pending request '{id_}': Server was stopped before getting the response"
                )

        if self.server_exit_callback is not None:
            await self.server_exit_callback()

        logger.debug(f"End of server stopped handler {self.readable_id}")

    def stop(self) -> None:
        self._stop_event.set()

    def _send_data(self, data: str):
        header = (
            f"Content-Length: {len(data)}\r\n"
            f"Content-Type: {self.CONTENT_TYPE}; charset={self.CHARSET}\r\n\r\n"
        )
        data = header + data

        try:
            self.writer.write(data.encode(self.CHARSET))
        except Exception as error:
            # the writer puts a message in the queue without size, so no exception
            # are expected. If one internal come such as shutdown exception because of
            # mistake in implementation, log it
            logger.error(f"Error sending data to {self.readable_id}:")
            logger.exception(error)

    def _send_error_response(
        self,
        request_id: str | int | None,
        code: int,
        message: str,
        data: typing.Any = None,
    ) -> None:
        """Send a JSON-RPC error response.

        Args:
            request_id: The ID of the request that caused the error. None for notifications.
            code: JSON-RPC error code (see JsonRpcErrorCode)
            message: Short description of the error
            data: Optional additional error information
        """
        error_object = {"code": code, "message": message}
        if data is not None:
            error_object["data"] = data

        response_dict = {
            "jsonrpc": self.VERSION,
            "id": request_id,
            "error": error_object,
        }

        response_str = json.dumps(response_dict)
        logger.debug(f"Sending error response: {code} - {message}")
        self._send_data(response_str)

    def notify(self, method: str, params: typing.Any | None = None) -> None:
        logger.debug(f"Sending notification: '{method}' {params}")

        try:
            notification_params_type = self.message_types[method][1]
        except KeyError:
            raise ValueError(f"Type of notification params for {method} not found")

        if notification_params_type is not None:
            notification_params_dict = apischema.serialize(
                notification_params_type, params, aliaser=apischema.utils.to_camel_case
            )
        else:
            notification_params_dict = None

        notification_dict = {
            "method": method,
            "params": notification_params_dict,
            "jsonrpc": self.VERSION,
        }

        try:
            notification_str = json.dumps(notification_dict)
        except (TypeError, ValueError) as error:
            raise InvalidResponse(
                f"Failed to serialize notification: {error}"
            ) from error
        logger.trace(notification_str)
        self._send_data(notification_str)

    def send_request_sync(
        self,
        method: str,
        params: typing.Any | None = None,
        # timeout: float | None = None
    ) -> concurrent.futures.Future[typing.Any]:
        try:
            request_params_type = self.message_types[method][1]
        except KeyError:
            raise ValueError(f"Type for method {method} not found")

        msg_id = str(uuid.uuid4())
        logger.debug(
            f'Sending request with id "{msg_id}": {method} to {self.readable_id}'
        )

        if request_params_type is not None:
            request_params_dict = apischema.serialize(
                request_params_type, params, aliaser=apischema.utils.to_camel_case
            )
        else:
            request_params_dict = None

        future = concurrent.futures.Future()
        try:
            self._expected_result_type_by_msg_id[msg_id] = self.message_types[method][2]
        except KeyError:
            raise ValueError(f"Message type not found for {method}")

        self._sync_request_futures[msg_id] = future

        request_dict = {
            "id": msg_id,
            "method": method,
            "params": request_params_dict,
            "jsonrpc": self.VERSION,
        }

        try:
            request_str = json.dumps(request_dict)
        except (TypeError, ValueError) as error:
            # Clean up the future if serialization fails
            self._sync_request_futures.pop(msg_id, None)
            self._expected_result_type_by_msg_id.pop(msg_id, None)
            raise InvalidResponse(f"Failed to serialize request: {error}") from error

        self._send_data(request_str)

        return future
        # try:
        #     response = future.result(
        #         timeout=timeout,
        #     )
        #     logger.debug(f"Got response on {method} from {self.readable_id}")
        #     return response
        # except TimeoutError:
        #     raise ResponseTimeout(
        #         f"Timeout {timeout}s for response on {method} to"
        #         f" {self.readable_id}"
        #     )

    async def send_request(
        self,
        method: str,
        params: typing.Any | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        try:
            request_params_type = self.message_types[method][1]
        except KeyError:
            raise ValueError(f"Type for method {method} not found")

        msg_id = str(uuid.uuid4())
        logger.debug(
            f'Sending request with id "{msg_id}": {method} to {self.readable_id}'
        )

        if request_params_type is not None:
            request_params_dict = apischema.serialize(
                request_params_type, params, aliaser=apischema.utils.to_camel_case
            )
        else:
            request_params_dict = None

        message_dict = {
            "id": msg_id,
            "method": method,
            "params": request_params_dict,
            "jsonrpc": self.VERSION,
        }

        # Serialize request to JSON
        try:
            request_str = json.dumps(message_dict)
        except (TypeError, ValueError) as error:
            raise InvalidResponse(f"Failed to serialize request: {error}") from error

        future = asyncio.Future()
        self._async_request_futures[msg_id] = future

        try:
            self._expected_result_type_by_msg_id[msg_id] = self.message_types[method][2]
        except KeyError:
            raise ValueError(f"Message type not found for {method}")

        self._send_data(request_str)

        try:
            response = await asyncio.wait_for(
                future,
                timeout,
            )
            logger.debug(f"Got response on {method} from {self.readable_id}")
            return response
        except TimeoutError:
            raise ResponseTimeout(
                f"Timeout {timeout}s for response on {method} to"
                f" runner {self.readable_id}"
            )
        except asyncio.CancelledError as error:
            raise RequestCancelledError(request_id=msg_id) from error

    async def process_incoming_messages(self) -> None:
        logger.debug(f"Start processing messages from server {self.readable_id}")
        try:
            while True:
                raw_message = await self.in_message_queue.async_q.get()
                if raw_message == QUEUE_END:
                    # TODO: this message doesn't come, task is always cancelled
                    logger.info("Queue with messages from server was closed")
                    self.in_message_queue.async_q.task_done()
                    self.in_message_queue.async_q.shutdown()
                    break

                try:
                    await self.handle_message(raw_message)
                except Exception as exc:
                    logger.exception(exc)
                finally:
                    self.in_message_queue.async_q.task_done()
        except asyncio.CancelledError:
            # logger.warning("process_incoming_messages was cancelled")
            ...

        # self.in_message_queue.async_q.shutdown()
        logger.debug(f"End processing messages from server {self.readable_id}")

    async def handle_message(self, message: dict[str, typing.Any]) -> None:
        if "id" in message:
            message_id = message["id"]

            if not isinstance(message_id, str) and not isinstance(message_id, int):
                logger.warning(
                    f"Got message with unsupported id: {type(message_id)}, but string or int expected"
                )
                return

            if "error" in message:
                # error as response
                logger.trace(f"Processing message with error on request {message_id}")
                # sync request futures are handled in another thread, handle only async
                # here
                if message_id not in self._async_request_futures:
                    logger.error(
                        f"Got error as response for {message_id}, but no response was expected"
                    )
                    return

                future = self._async_request_futures.pop(message_id, None)
                if future is None:
                    logger.error(
                        f"Got error on request {message_id}, but no response was expected"
                    )
                    return

                try:
                    response_error = apischema.deserialize(
                        ResponseError,
                        data=message["error"],
                        aliaser=apischema.utils.to_camel_case,
                    )
                except apischema.ValidationError as error:
                    exception = InvalidResponse(". ".join(error.messages))

                    # avoid race condition: request is sent, then cancelled and the server
                    # sends the response before processing the cancel notification
                    if not future.cancelled():
                        future.set_exception(exception)
                    return

                exception = ErrorOnRequest(error=response_error)
                # avoid race condition: request is sent, then cancelled and the server
                # sends the response before processing the cancel notification
                if not future.cancelled():
                    future.set_exception(exception)
                return
            elif "method" in message:
                # incoming request
                logger.trace(
                    f"Processing message with incoming request {message['method']} | {self.readable_id}"
                )
                try:
                    request_type = self.message_types[message["method"]][0]
                    result_type = self.message_types[message["method"]][3]
                except KeyError:
                    # Method type not registered - send 'Method not found' error
                    logger.warning(
                        f"Received request for unregistered method: {message.get('method')} | {self.readable_id}"
                    )
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                        message="Method not found",
                        data=f"Method '{message.get('method')}' is not registered",
                    )
                    return

                try:
                    request = apischema.deserialize(
                        request_type, message, aliaser=apischema.utils.to_camel_case
                    )
                except apischema.ValidationError as error:
                    # Invalid request parameters - send 'Invalid params' error
                    logger.warning(
                        f"Invalid params for method {message.get('method')}: {error.messages} | {self.readable_id}"
                    )
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.INVALID_PARAMS,
                        message="Invalid params",
                        data=". ".join(error.messages),
                    )
                    return

                method = message["method"]
                if method not in self.feature_impls:
                    # Method implementation not found - send 'Method not found' error
                    logger.warning(
                        f"Received request for unsupported method: {method} | {self.readable_id}"
                    )
                    self._send_error_response(
                        request_id=message_id,
                        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                        message="Method not found",
                        data=f"Method '{method}' is not supported",
                    )
                    return

                impl = self.feature_impls[method]
                new_task = asyncio.create_task(
                    self.run_feature_impl(message_id, impl(request.params), result_type)
                )
                self._async_tasks.append(new_task)
            else:
                # response on our request
                logger.trace(
                    f"Processing message with response to request {message_id}"
                )
                if message_id not in self._async_request_futures:
                    logger.error(
                        f"Got response to {message_id}, but no response was expected"
                    )
                    return

                # sync request futures are handled in another thread, handle only async
                # here
                future = self._async_request_futures.pop(message_id, None)
                if future is None:
                    logger.error(
                        f"Got response to {message_id}, but no response was expected"
                    )
                    return

                result_type = self._expected_result_type_by_msg_id[message_id]
                try:
                    response = apischema.deserialize(
                        result_type, message, aliaser=apischema.utils.to_camel_case
                    )
                except apischema.ValidationError as error:
                    logger.error("errro")
                    logger.exception(error)
                    exception = InvalidResponse(". ".join(error.messages))

                    # avoid race condition: request is sent, then cancelled and the server
                    # sends the response before processing the cancel notification
                    if not future.cancelled():
                        future.set_exception(exception)
                    return

                # avoid race condition: request is sent, then cancelled and the server
                # sends the response before processing the cancel notification
                if not future.cancelled():
                    future.set_result(response)
                    logger.trace(f"Successfully processed response to {message_id}")
                else:
                    logger.trace(
                        f"Request {message_id} was cancelled, ignore the response"
                    )
                return
        else:
            # incoming notification
            logger.trace(
                f"Processing message with incoming notification | {self.readable_id}"
            )
            if "method" not in message:
                logger.error(
                    f"Notification expected to have a 'method' field: {message} | {self.readable_id}"
                )
                return

            method = message["method"]
            if method not in self.feature_impls:
                logger.warning(
                    f"Got notification {method}, but it is not supported | {self.readable_id}"
                )
                return

            if method not in self.message_types:
                logger.warning(f"Got unsupported notification: {method}")
                return

            try:
                notification_type = self.message_types[method][0]
                notification = apischema.deserialize(
                    notification_type, message, aliaser=apischema.utils.to_camel_case
                )
            except (KeyError, apischema.ValidationError) as error:
                logger.warning(
                    f"Failed to deserialize notification {method}: {error} | {self.readable_id}"
                )
                # For notifications, we don't send error responses
                return

            impl = self.feature_impls[method]
            new_task = asyncio.create_task(
                self.run_notification_impl(impl(notification.params))
            )
            self._async_tasks.append(new_task)

    async def run_feature_impl(
        self, message_id: str | int, impl_coro, result_type
    ) -> None:
        try:
            result = await impl_coro

            logger.trace(f"{result_type} {result}")
            # Send successful response back to the server
            response_dict = {
                "jsonrpc": self.VERSION,
                "id": message_id,
                "result": apischema.serialize(
                    result_type, result, aliaser=apischema.utils.to_camel_case
                ),
            }

            response_str = json.dumps(response_dict)
            logger.debug(f"Sending response for request {message_id}")
            self._send_data(response_str)
        except Exception as exception:
            logger.warning(
                f"Error occured on running handler of message {message_id} | {self.readable_id}"
            )
            logger.exception(exception)
            self._send_error_response(
                request_id=message_id,
                code=JsonRpcErrorCode.INTERNAL_ERROR,
                message="Internal error",
                data="",
            )
        finally:
            current_task = asyncio.current_task()
            try:
                self._async_tasks.remove(current_task)
            except ValueError:
                ...

    async def run_notification_impl(self, impl_coro) -> None:
        try:
            await impl_coro
        except Exception as exception:
            logger.warning(
                f"Error occured on running handler of message | {self.readable_id}"
            )
            logger.exception(exception)
        finally:
            current_task = asyncio.current_task()
            try:
                self._async_tasks.remove(current_task)
            except ValueError:
                ...

    async def _connect_to_server_io(self, timeout: float | None) -> None:
        if self.communication_type == CommunicationType.TCP:
            assert self._tcp_port_future is not None

            try:
                await asyncio.wait_for(self._tcp_port_future, timeout)
            except TimeoutError as exception:
                for task in self._async_tasks_in_io_thread:
                    task.cancel()

                raise RunnerFailedToStart("Didn't get port in 30 seconds") from exception

            port = self._tcp_port_future.result()
            logger.debug(f"Got port {port} | {self.readable_id}")

            try:
                self._reader, self._writer = await asyncio.open_connection("127.0.0.1", port)
            except Exception as exception:
                logger.exception(exception)

                for task in self._async_tasks_in_io_thread:
                    task.cancel()

                raise RunnerFailedToStart(f"Failed to open connection: {exception}") from exception

        assert self._reader is not None and self._writer is not None

        task = asyncio.create_task(
            read_messages_from_reader(
                self._reader,
                self.in_message_queue.sync_q,
                self._sync_request_futures,
                self._expected_result_type_by_msg_id,
                self._stop_event,
                server_id=self.readable_id,
            )
        )
        task.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"read_messages_from_reader|{self.readable_id}"
            )
        )
        self._async_tasks_in_io_thread.append(task)
        
        task = asyncio.create_task(
            send_messages_from_queue(queue=self.out_message_queue.async_q, writer=self._writer)
        )
        task.add_done_callback(
            functools.partial(
                task_done_log_callback, task_id=f"send_messages_from_queue|{self.readable_id}"
            )
        )
        self._async_tasks_in_io_thread.append(task)


async def start_server(
    cmd: str,
    communication_type: CommunicationType,
    out_message_queue: culsans.Queue[bytes],
    stop_event: threading.Event,
    server_stopped_event: threading.Event,
    server_id: str,
    async_tasks: list[asyncio.Task[typing.Any]],
    debug_port_future: concurrent.futures.Future[int] | None
) -> tuple[asyncio.StreamReader | None, asyncio.StreamWriter | None, asyncio.Future[int] | None]:
    logger.debug(f"Starting server process: {cmd}")
    
    creationflags = 0
    # start_new_session = True .. process has parent id of real parent, but is not
    #                             ended if parent was ended
    start_new_session = True
    if sys.platform == "win32":
        # use creationflags because `start_new_session` doesn't work on Windows
        # subprocess.CREATE_NO_WINDOW .. no console window on Windows. TODO: test
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        start_new_session = False

    subprocess_kwargs = {
        "creationflags": creationflags,
        "start_new_session": start_new_session,
    }

    # Start subprocess with appropriate stdio configuration
    if communication_type == CommunicationType.STDIO:
        server = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # max length of line: in STDIO mode, the whole file can be sent as a single
            # line, increase default limit 64 KBit to 10 MiB
            limit = 1024 * 1024 * 10,  # 10 MiB,
            **subprocess_kwargs,
        )
    elif communication_type == CommunicationType.TCP:
        server = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_kwargs,
        )
    else:
        raise ValueError(f"Unsupported communication type: {communication_type}")

    logger.debug(f"{server_id} - process id: {server.pid}")

    task = asyncio.create_task(log_stderr(server.stderr, stop_event))
    task.add_done_callback(
        functools.partial(task_done_log_callback, task_id=f"log_stderr|{server_id}")
    )
    async_tasks.append(task)

    # Get reader and writer based on communication type
    if communication_type == CommunicationType.STDIO:
        reader = server.stdout
        writer = server.stdin
        tcp_port_future = None
    else:  # CommunicationType.TCP
        reader = None
        writer = None

        # TODO: read debug port also in stdio
        tcp_port_future = asyncio.Future[int]()
        task = asyncio.create_task(
            read_stdout(server.stdout, stop_event, tcp_port_future, server.pid, debug_port_future)
        )
        task.add_done_callback(
            functools.partial(task_done_log_callback, task_id=f"read_stdout|{server_id}")
        )
        async_tasks.append(task)

        logger.debug(f"Wait for port of {server.pid} | {server_id}")

    task = asyncio.create_task(
        wait_for_stop_event_and_clean(
            stop_event, server, async_tasks, server_stopped_event, out_message_queue.async_q
        )
    )
    task.add_done_callback(
        functools.partial(
            task_done_log_callback, task_id=f"wait_for_stop_event_and_clean|{server_id}"
        )
    )

    logger.debug(f"Server {server.pid} started with {communication_type.name} | {server_id}")
    
    return (reader, writer, tcp_port_future)


async def wait_for_stop_event_and_clean(
    stop_event: threading.Event,
    server_process: asyncio.subprocess.Process,
    tasks: list[asyncio.Task[typing.Any]],
    server_stopped_event: threading.Event,
    out_message_queue: culsans.AsyncQueue[bytes],
) -> None:
    # wait either on stop event (=user asks to stop the client) or end of the server
    # process
    logger.debug("Wait on one of tasks")
    tasks_to_wait = [
        asyncio.create_task(asyncio.to_thread(stop_event.wait)),
        asyncio.create_task(server_process.wait()),
    ]
    _, _ = await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
    logger.debug("One of tasks to wait is done")

    if not stop_event.is_set():
        stop_event.set()

    # close the WriterFromQueue
    await out_message_queue.put(QUEUE_END)

    if server_process.returncode is None:
        logger.debug("Wait for the end of server process")
        _ = await server_process.wait()
        logger.debug(f"Server process ended with code {server_process.returncode}")

    for task in tasks:
        if not task.done():
            task.cancel()

    server_stopped_event.set()
    logger.debug("Cleaned resources of client")


async def log_stderr(stderr: asyncio.StreamReader, stop_event: threading.Event) -> None:
    """Read and log stderr output from the subprocess."""
    logger.debug("Start reading logs from stderr")
    try:
        while not stop_event.is_set():
            line = await stderr.readline()
            if not line:
                break
            logger.debug(
                f"Server stderr: {line.decode('utf-8', errors='replace').rstrip()}"
            )
    except asyncio.CancelledError:
        pass

    logger.debug("End reading logs from stderr")


async def read_stdout(
    stdout: asyncio.StreamReader,
    stop_event: threading.Event,
    port_future: asyncio.Future[int],
    server_pid: int,
    debug_port_future: concurrent.futures.Future[int] | None
) -> None:
    logger.debug(f"Start reading logs from stdout | {server_pid}")
    try:
        while not stop_event.is_set():
            try:
                line = await stdout.readline()
            except ValueError as exception:
                logger.error(exception)
                continue

            if not line:
                break
            if b"Serving on (" in line:
                match = re.search(rb"Serving on \('[\d.]+', (\d+)\)", line)
                if match:
                    port = int(match.group(1))
                    if not port_future.done():
                        port_future.set_result(port)
            elif b"Debug session:" in line:
                match = re.search(rb"Debug session: [\d.]+:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    if debug_port_future is not None and not debug_port_future.done():
                        debug_port_future.set_result(port)
            # logger.debug(
            #     f"Server {server_pid} stdout: {line.decode('utf-8', errors='replace').rstrip()}"
            # )
    except asyncio.CancelledError:
        pass
    # except Exception as exception:
    #     # catch all unexpected exception to log them properly and to get explicit log
    #     # about end of reading
    #     logger.exception(exception)

    logger.debug(f"End reading logs from stdout | {server_pid}")


async def send_messages_from_queue(
    queue: culsans.AsyncQueue[bytes], writer: asyncio.StreamWriter
) -> None:
    logger.debug("Start sending messages from queue")

    try:
        while True:
            message = await queue.get()
            if message == QUEUE_END:
                writer.close()
                logger.debug("Queue was closed, stop sending")
                break
            writer.write(message)
            await writer.drain()
    except asyncio.CancelledError:
        ...

    queue.shutdown()
    logger.debug("End sending messages from queue")


CONTENT_LENGTH_PATTERN = re.compile(rb"^Content-Length: (\d+)\r\n$")


async def read_messages_from_reader(
    reader: asyncio.StreamReader,
    message_queue: culsans.SyncQueue[bytes],
    request_futures: dict[str, concurrent.futures.Future[typing.Any]],
    result_types: dict[str, typing.Any],
    stop_event: threading.Event,
    server_id: str,
) -> None:
    content_length = 0

    try:
        while not stop_event.is_set():
            try:
                try:
                    header = await reader.readline()
                except ValueError:
                    logger.error(f"Value error in readline of {server_id}")
                    continue
                except ConnectionResetError:
                    logger.warning(
                        f"Server {server_id} closed the connection(ConnectionResetError), stop the client"
                    )
                    stop_event.set()
                    break

                if not header:
                    if reader.at_eof():
                        logger.debug(f"Reader reached EOF | {server_id}")
                        break
                    continue

                # Extract content length if possible
                if not content_length:
                    match = CONTENT_LENGTH_PATTERN.fullmatch(header)
                    if match:
                        content_length = int(match.group(1))
                        logger.debug(f"Content length | {server_id}: {content_length}")
                    else:
                        logger.debug(
                            f"Not matched content length: {header} | {server_id}"
                        )

                # Check if all headers have been read (as indicated by an empty line \r\n)
                if content_length and not header.strip():
                    # Read body
                    body = None
                    try:
                        body = await reader.readexactly(content_length)
                    except asyncio.IncompleteReadError as error:
                        logger.debug(
                            f"Incomplete read error: {error} | {server_id} : {error.partial}"
                        )
                        content_length = 0
                        continue
                    except ConnectionResetError:
                        logger.warning(
                            f"Server {server_id} closed the connection(ConnectionResetError), stop the client"
                        )
                        stop_event.set()
                        break

                    if not body:
                        content_length = 0
                        continue

                    logger.debug(f"Got content {server_id}: {body}")
                    try:
                        message = json.loads(body)
                    except json.JSONDecodeError as exc:
                        logger.error(
                            f"Failed to parse JSON message: {exc} | {server_id}"
                        )
                        continue
                    finally:
                        # Reset
                        content_length = 0

                    if not isinstance(message, dict):
                        logger.error("JSON Message expected to be a dict")
                        continue
                    if "jsonrpc" not in message:
                        logger.error("JSON Message expected to contain 'jsonrpc' key")
                        continue

                    if message["jsonrpc"] != JsonRpcClient.VERSION:
                        logger.warning(f'Unknown message "{message}" | {server_id}')
                        continue

                    # error should be also handled here
                    is_response = (
                        "id" in message
                        and "error" not in message
                        and "method" not in message
                    )

                    if is_response:
                        logger.debug(f"Response message received. | {server_id}")
                        msg_id = message["id"]
                        raw_result = message.get("result", None)
                        future = request_futures.pop(msg_id, None)

                        if future is not None:
                            try:
                                result_type = result_types[msg_id]
                            except KeyError:
                                logger.error(
                                    f"Result type not found for message {msg_id}"
                                )
                                continue

                            try:
                                result = apischema.deserialize(
                                    result_type,
                                    raw_result,
                                    aliaser=apischema.utils.to_camel_case,
                                )
                            except apischema.ValidationError as error:
                                exception = InvalidResponse(". ".join(error.messages))
                                if not future.cancelled():
                                    future.set_exception(exception)
                                continue

                            logger.debug(
                                f'Received result for message "{msg_id}" | {server_id}'
                            )
                            if not future.cancelled():
                                future.set_result(result)
                        else:
                            message_queue.put(message)
                    else:
                        # incoming request or notification
                        message_queue.put(message)
                else:
                    if not header.startswith(
                        b"Content-Length:"
                    ) and not header.startswith(b"Content-Type:"):
                        logger.debug(
                            f'Something is wrong: {content_length} "{header}" {not header.strip()} | {server_id}'
                        )
            except Exception as exc:
                logger.exception(
                    f"Exception in message reader loop | {server_id}: {exc}"
                )
                # Reset state to avoid infinite loop on persistent errors
                content_length = 0
    except asyncio.CancelledError:
        ...

    message_queue.put_nowait(QUEUE_END)
    logger.debug(f"End reading messages from reader | {server_id}")


__all__ = ["JsonRpcClient"]
