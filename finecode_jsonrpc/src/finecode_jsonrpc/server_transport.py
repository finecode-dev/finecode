"""Server-side JSON-RPC transports.

These transports receive connections (from stdin/stdout or TCP) rather than
spawning subprocesses like :class:`StdioTransport`.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import re
import sys
import typing

from loguru import logger

from ._frames import OutgoingFrame
from ._hop_guard import call_guarded
from .tracing import (
    HopCloseReason,
    HopStage,
    HopToken,
    IHopObserver,
    StageStatus,
    StageTime,
    capture_stage_time,
)

CONTENT_LENGTH_PATTERN = re.compile(rb"^Content-Length: (\d+)\r\n$")
CHARSET = "utf-8"
CONTENT_TYPE = "application/vscode-jsonrpc"


class ServerStdioTransport:
    """Server-side transport that reads from own stdin and writes to own stdout.

    Unlike :class:`StdioTransport`, no subprocess is spawned.  The transport
    attaches to the process's own stdin/stdout pipes, making it suitable for
    an LSP / JSON-RPC server that is invoked by a client process.

    Reading uses short timeouts so that :meth:`stop` can interrupt the loop
    gracefully without waiting for the next byte from stdin.

    *framing* controls the wire format:

    - ``"content-length"`` (default): LSP-style ``Content-Length`` header framing.
    - ``"newline"``: newline-delimited JSON — one JSON object per line, no headers.
      Required by the MCP stdio transport spec.
    """

    def __init__(self, readable_id: str = "", framing: str = "content-length") -> None:
        self._readable_id = readable_id
        self._framing = framing
        self._stop_event = asyncio.Event()
        self._out_queue: asyncio.Queue[OutgoingFrame | None] = asyncio.Queue()
        self._on_message: (
            collections.abc.Callable[
                [dict[str, typing.Any]], collections.abc.Awaitable[None]
            ]
            | None
        ) = None
        self._on_message_timed: (
            collections.abc.Callable[
                [dict[str, typing.Any], StageTime],
                collections.abc.Awaitable[None],
            ]
            | None
        ) = None
        self._hop_observer: IHopObserver | None = None
        self._on_exit: (
            collections.abc.Callable[[], collections.abc.Awaitable[None]] | None
        ) = None
        self._tasks: list[asyncio.Task[typing.Any]] = []
        self._read_transport: asyncio.BaseTransport | None = None
        self._write_transport: asyncio.BaseTransport | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Configuration (call before start)
    # ------------------------------------------------------------------

    def on_message(
        self,
        handler: collections.abc.Callable[
            [dict[str, typing.Any]], collections.abc.Awaitable[None]
        ],
    ) -> None:
        self._on_message = handler

    def on_message_timed(
        self,
        handler: collections.abc.Callable[
            [dict[str, typing.Any], StageTime],
            collections.abc.Awaitable[None],
        ],
    ) -> None:
        """Register a frame-time-aware message handler (wins over on_message)."""
        self._on_message_timed = handler

    def attach_hop_observer(self, observer: IHopObserver | None) -> None:
        self._hop_observer = observer

    def on_exit(
        self,
        handler: collections.abc.Callable[[], collections.abc.Awaitable[None]],
    ) -> None:
        self._on_exit = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        stdin_buf: typing.BinaryIO | None = None,
        stdout_buf: typing.BinaryIO | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        stdin_buf = stdin_buf or sys.stdin.buffer
        stdout_buf = stdout_buf or sys.stdout.buffer

        # Wrap stdin as an asyncio StreamReader via connect_read_pipe
        reader = asyncio.StreamReader(limit=1024 * 1024 * 10)
        read_protocol = asyncio.StreamReaderProtocol(reader)
        self._read_transport, _ = await self._loop.connect_read_pipe(
            lambda: read_protocol, stdin_buf
        )

        # Wrap stdout as a write transport via connect_write_pipe
        self._write_transport, _ = await self._loop.connect_write_pipe(
            asyncio.BaseProtocol, stdout_buf
        )

        self._tasks.append(
            asyncio.create_task(
                self._read_messages(reader),
                name=f"server_read_messages|{self._readable_id}",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._write_messages(self._write_transport),
                name=f"server_write_messages|{self._readable_id}",
            )
        )

    async def stop(self) -> None:
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        await self._out_queue.put(None)

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._read_transport is not None:
            self._read_transport.close()

        logger.debug(f"ServerStdioTransport stopped | {self._readable_id}")

    # ------------------------------------------------------------------
    # Send (thread-safe)
    # ------------------------------------------------------------------

    def send(
        self, message: dict[str, typing.Any], *, hop: HopToken | None = None
    ) -> None:
        """Serialize *message* and enqueue for writing, tracking *hop* stages."""
        if self._framing == "newline":
            data = (json.dumps(message) + "\n").encode(CHARSET)
        else:
            body = json.dumps(message)
            header = (
                f"Content-Length: {len(body)}\r\n"
                f"Content-Type: {CONTENT_TYPE}; charset={CHARSET}\r\n\r\n"
            )
            data = (header + body).encode(CHARSET)

        frame = OutgoingFrame(data=data, hop=hop)
        if hop is not None:
            self._hop_stage(hop, "submitted", capture_stage_time())
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._insert_frame, frame)
        else:
            self._insert_frame(frame)

    def _insert_frame(self, frame: OutgoingFrame) -> None:
        """Queue *frame*; runs on the loop so ``enqueued`` means inserted."""
        self._out_queue.put_nowait(frame)
        if frame.hop is not None:
            self._hop_stage(frame.hop, "enqueued", capture_stage_time())

    def _hop_stage(
        self,
        hop: HopToken | None,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.stage(hop, stage, at, status))

    def _hop_close(self, hop: HopToken | None, reason: HopCloseReason) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(
            observer,
            lambda: observer.close_receiver(hop, reason, capture_stage_time()),
        )

    async def _dispatch_message(
        self, message: dict[str, typing.Any], frame_read: StageTime
    ) -> None:
        try:
            if self._on_message_timed is not None:
                await self._on_message_timed(message, frame_read)
            elif self._on_message is not None:
                await self._on_message(message)
        except Exception as exc:
            logger.exception(f"Error in message handler | {self._readable_id}: {exc}")

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _write_messages(self, write_transport: asyncio.BaseTransport) -> None:
        logger.debug(f"Start writing messages | {self._readable_id}")
        try:
            while True:
                item = await self._out_queue.get()
                if item is None:
                    break
                hop = item.hop
                if hop is not None:
                    self._hop_stage(hop, "dequeued", capture_stage_time())
                try:
                    write_transport.write(item.data)
                except Exception:
                    if hop is not None:
                        self._hop_stage(
                            hop, "write_returned", capture_stage_time(), "failed"
                        )
                        self._hop_close(hop, "write_failed")
                    raise
                if hop is not None:
                    self._hop_stage(hop, "write_returned", capture_stage_time())
                    self._hop_stage(hop, "drain_started", None, "unsupported")
                    self._hop_stage(hop, "drain_returned", None, "unsupported")
                    self._hop_close(hop, "completed")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error writing message | {self._readable_id}: {exc}")
        logger.debug(f"End writing messages | {self._readable_id}")

    async def _read_messages(self, reader: asyncio.StreamReader) -> None:
        """Read messages from stdin with short timeouts to allow graceful stop."""
        logger.debug(f"Start reading messages | {self._readable_id}")

        if self._framing == "newline":
            await self._read_messages_newline(reader)
        else:
            await self._read_messages_content_length(reader)

        logger.debug(f"End reading messages | {self._readable_id}")

        if self._on_exit is not None:
            try:
                await self._on_exit()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error in exit handler | {self._readable_id}: {exc}")

    async def _read_messages_newline(self, reader: asyncio.StreamReader) -> None:
        """Read newline-delimited JSON messages (MCP stdio transport)."""
        try:
            while not self._stop_event.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=0.1)
                except TimeoutError:
                    continue
                except (ValueError, ConnectionResetError) as exc:
                    logger.warning(f"Read error | {self._readable_id}: {exc}")
                    break

                frame_read = capture_stage_time()

                if not line:
                    if reader.at_eof():
                        logger.debug(f"Reader reached EOF | {self._readable_id}")
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.error(f"JSON parse error | {self._readable_id}: {exc}")
                    continue

                if not isinstance(message, dict):
                    logger.error(f"Expected dict message | {self._readable_id}")
                    continue

                await self._dispatch_message(message, frame_read)
        except asyncio.CancelledError:
            pass

    async def _read_messages_content_length(self, reader: asyncio.StreamReader) -> None:
        """Read Content-Length-framed JSON messages (LSP-style)."""
        content_length = 0

        try:
            while not self._stop_event.is_set():
                try:
                    header = await asyncio.wait_for(reader.readline(), timeout=0.1)
                except TimeoutError:
                    continue
                except (ValueError, ConnectionResetError) as exc:
                    logger.warning(f"Read error | {self._readable_id}: {exc}")
                    break

                if not header:
                    if reader.at_eof():
                        logger.debug(f"Reader reached EOF | {self._readable_id}")
                        break
                    continue

                if not content_length:
                    match = CONTENT_LENGTH_PATTERN.fullmatch(header)
                    if match:
                        content_length = int(match.group(1))
                    continue

                if content_length and not header.strip():
                    try:
                        body = await reader.readexactly(content_length)
                    except asyncio.IncompleteReadError as exc:
                        logger.debug(f"Incomplete read | {self._readable_id}: {exc}")
                        content_length = 0
                        continue
                    except ConnectionResetError:
                        logger.warning(f"Connection reset | {self._readable_id}")
                        break

                    content_length = 0

                    if not body:
                        continue

                    frame_read = capture_stage_time()

                    try:
                        message = json.loads(body)
                    except json.JSONDecodeError as exc:
                        logger.error(f"JSON parse error | {self._readable_id}: {exc}")
                        continue

                    if not isinstance(message, dict):
                        logger.error(f"Expected dict message | {self._readable_id}")
                        continue

                    await self._dispatch_message(message, frame_read)
        except asyncio.CancelledError:
            pass


class TcpServerTransport:
    """Server-side transport wrapping a TCP connection accepted by asyncio.start_server.

    Usage::

        async def handle_connection(reader, writer):
            transport = TcpServerTransport(reader, writer)
            transport.on_message(...)
            await transport.start()
            # transport runs until stop() or client disconnects
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        readable_id: str = "",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._readable_id = readable_id
        self._stop_event = asyncio.Event()
        self._out_queue: asyncio.Queue[OutgoingFrame | None] = asyncio.Queue()
        self._on_message: (
            collections.abc.Callable[
                [dict[str, typing.Any]], collections.abc.Awaitable[None]
            ]
            | None
        ) = None
        self._on_message_timed: (
            collections.abc.Callable[
                [dict[str, typing.Any], StageTime],
                collections.abc.Awaitable[None],
            ]
            | None
        ) = None
        self._hop_observer: IHopObserver | None = None
        self._on_exit: (
            collections.abc.Callable[[], collections.abc.Awaitable[None]] | None
        ) = None
        self._tasks: list[asyncio.Task[typing.Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def on_message(
        self,
        handler: collections.abc.Callable[
            [dict[str, typing.Any]], collections.abc.Awaitable[None]
        ],
    ) -> None:
        self._on_message = handler

    def on_message_timed(
        self,
        handler: collections.abc.Callable[
            [dict[str, typing.Any], StageTime],
            collections.abc.Awaitable[None],
        ],
    ) -> None:
        """Register a frame-time-aware message handler (wins over on_message)."""
        self._on_message_timed = handler

    def attach_hop_observer(self, observer: IHopObserver | None) -> None:
        self._hop_observer = observer

    def on_exit(
        self,
        handler: collections.abc.Callable[[], collections.abc.Awaitable[None]],
    ) -> None:
        self._on_exit = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks.append(
            asyncio.create_task(
                self._read_messages(self._reader),
                name=f"tcp_read_messages|{self._readable_id}",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._write_messages(self._writer),
                name=f"tcp_write_messages|{self._readable_id}",
            )
        )

    async def stop(self) -> None:
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        await self._out_queue.put(None)

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        with contextlib.suppress(Exception):
            self._writer.close()

        logger.debug(f"TcpServerTransport stopped | {self._readable_id}")

    # ------------------------------------------------------------------
    # Send (call from asyncio loop only — no thread-safe wrapper needed)
    # ------------------------------------------------------------------

    def send(
        self, message: dict[str, typing.Any], *, hop: HopToken | None = None
    ) -> None:
        """Serialize *message* and enqueue for writing, tracking *hop* stages."""
        body = json.dumps(message)
        header = (
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: {CONTENT_TYPE}; charset={CHARSET}\r\n\r\n"
        )
        data = (header + body).encode(CHARSET)

        frame = OutgoingFrame(data=data, hop=hop)
        if hop is not None:
            self._hop_stage(hop, "submitted", capture_stage_time())
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._insert_frame, frame)
        else:
            self._insert_frame(frame)

    def _insert_frame(self, frame: OutgoingFrame) -> None:
        """Queue *frame*; runs on the loop so ``enqueued`` means inserted."""
        self._out_queue.put_nowait(frame)
        if frame.hop is not None:
            self._hop_stage(frame.hop, "enqueued", capture_stage_time())

    def _hop_stage(
        self,
        hop: HopToken | None,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(observer, lambda: observer.stage(hop, stage, at, status))

    def _hop_close(self, hop: HopToken | None, reason: HopCloseReason) -> None:
        observer = self._hop_observer
        if hop is None or observer is None:
            return
        call_guarded(
            observer,
            lambda: observer.close_receiver(hop, reason, capture_stage_time()),
        )

    async def _dispatch_message(
        self, message: dict[str, typing.Any], frame_read: StageTime
    ) -> None:
        try:
            if self._on_message_timed is not None:
                await self._on_message_timed(message, frame_read)
            elif self._on_message is not None:
                await self._on_message(message)
        except Exception as exc:
            logger.exception(f"Error in message handler | {self._readable_id}: {exc}")

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _write_messages(self, writer: asyncio.StreamWriter) -> None:
        logger.debug(f"Start writing messages | {self._readable_id}")
        try:
            while True:
                item = await self._out_queue.get()
                if item is None:
                    break
                hop = item.hop
                if hop is not None:
                    self._hop_stage(hop, "dequeued", capture_stage_time())
                try:
                    writer.write(item.data)
                except Exception:
                    if hop is not None:
                        self._hop_stage(
                            hop, "write_returned", capture_stage_time(), "failed"
                        )
                        self._hop_close(hop, "write_failed")
                    raise
                if hop is not None:
                    self._hop_stage(hop, "write_returned", capture_stage_time())
                    self._hop_stage(hop, "drain_started", capture_stage_time())
                try:
                    await writer.drain()
                except Exception:
                    if hop is not None:
                        self._hop_stage(
                            hop, "drain_returned", capture_stage_time(), "failed"
                        )
                        self._hop_close(hop, "write_failed")
                    raise
                if hop is not None:
                    self._hop_stage(hop, "drain_returned", capture_stage_time())
                    self._hop_close(hop, "completed")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error writing message | {self._readable_id}: {exc}")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
        logger.debug(f"End writing messages | {self._readable_id}")

    async def _read_messages(self, reader: asyncio.StreamReader) -> None:
        logger.debug(f"Start reading messages | {self._readable_id}")
        content_length = 0

        try:
            while not self._stop_event.is_set():
                try:
                    header = await reader.readline()
                except (ValueError, ConnectionResetError) as exc:
                    logger.warning(f"Read error | {self._readable_id}: {exc}")
                    break

                if not header:
                    if reader.at_eof():
                        logger.debug(f"Reader reached EOF | {self._readable_id}")
                        break
                    continue

                if not content_length:
                    match = CONTENT_LENGTH_PATTERN.fullmatch(header)
                    if match:
                        content_length = int(match.group(1))
                    continue

                if content_length and not header.strip():
                    try:
                        body = await reader.readexactly(content_length)
                    except asyncio.IncompleteReadError as exc:
                        logger.debug(f"Incomplete read | {self._readable_id}: {exc}")
                        content_length = 0
                        continue
                    except ConnectionResetError:
                        logger.warning(f"Connection reset | {self._readable_id}")
                        break

                    content_length = 0

                    if not body:
                        continue

                    frame_read = capture_stage_time()

                    try:
                        message = json.loads(body)
                    except json.JSONDecodeError as exc:
                        logger.error(f"JSON parse error | {self._readable_id}: {exc}")
                        continue

                    if not isinstance(message, dict):
                        logger.error(f"Expected dict message | {self._readable_id}")
                        continue

                    await self._dispatch_message(message, frame_read)
        except asyncio.CancelledError:
            pass

        logger.debug(f"End reading messages | {self._readable_id}")

        # Signal that the connection has ended (client disconnect, EOF, or error)
        # so that any caller polling _stop_event can unblock.
        self._stop_event.set()

        if self._on_exit is not None:
            try:
                await self._on_exit()
            except Exception as exc:
                logger.exception(f"Error in exit handler | {self._readable_id}: {exc}")
