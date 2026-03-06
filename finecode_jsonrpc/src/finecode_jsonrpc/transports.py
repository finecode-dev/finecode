from __future__ import annotations

import asyncio
import collections.abc
import json
import re
import sys
import subprocess  # needed for windows
import typing
from pathlib import Path

from loguru import logger

CONTENT_LENGTH_PATTERN = re.compile(rb"^Content-Length: (\d+)\r\n$")
CHARSET = "utf-8"
CONTENT_TYPE = "application/vscode-jsonrpc"


class StdioTransport:
    """Raw JSON-RPC transport over subprocess STDIO with Content-Length framing.

    All I/O runs on the event loop where ``start()`` is called.
    ``send()`` is thread-safe (writes go through an ``asyncio.Queue``).
    """

    def __init__(self, readable_id: str = "") -> None:
        self._readable_id = readable_id
        self._process: asyncio.subprocess.Process | None = None
        self._stop_event = asyncio.Event()
        self._out_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._on_message: (
            collections.abc.Callable[
                [dict[str, typing.Any]], collections.abc.Awaitable[None]
            ]
            | None
        ) = None
        self._on_exit: (
            collections.abc.Callable[[], collections.abc.Awaitable[None]] | None
        ) = None
        self._tasks: list[asyncio.Task[typing.Any]] = []
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
        cmd: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()

        creationflags = 0
        start_new_session = True
        if sys.platform == "win32":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            start_new_session = False

        self._process = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=1024 * 1024 * 10,  # 10 MiB
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

        logger.debug(
            f"StdioTransport started process pid={self._process.pid} | {self._readable_id}"
        )

        assert self._process.stdout is not None
        assert self._process.stdin is not None
        assert self._process.stderr is not None

        self._tasks.append(
            asyncio.create_task(
                self._read_messages(self._process.stdout),
                name=f"read_messages|{self._readable_id}",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._write_messages(self._process.stdin),
                name=f"write_messages|{self._readable_id}",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._log_stderr(self._process.stderr),
                name=f"log_stderr|{self._readable_id}",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._wait_for_exit(),
                name=f"wait_for_exit|{self._readable_id}",
            )
        )

    async def stop(self) -> None:
        if self._stop_event.is_set():
            return

        self._stop_event.set()

        # Signal writer to stop
        await self._out_queue.put(None)

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()

        logger.debug(f"StdioTransport stopped | {self._readable_id}")

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and not self._stop_event.is_set()
        )

    # ------------------------------------------------------------------
    # Send (thread-safe)
    # ------------------------------------------------------------------

    def send(self, message: dict[str, typing.Any]) -> None:
        """Serialize *message* to JSON with Content-Length header and enqueue.

        Safe to call from any thread.
        """
        body = json.dumps(message)
        header = (
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: {CONTENT_TYPE}; charset={CHARSET}\r\n\r\n"
        )
        data = (header + body).encode(CHARSET)

        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._out_queue.put_nowait, data)
        else:
            self._out_queue.put_nowait(data)

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _write_messages(self, stdin: asyncio.StreamWriter) -> None:
        logger.debug(f"Start writing messages | {self._readable_id}")
        try:
            while True:
                data = await self._out_queue.get()
                if data is None:
                    break
                stdin.write(data)
                await stdin.drain()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error writing message | {self._readable_id}: {exc}")
        finally:
            try:
                stdin.close()
            except Exception:
                pass
        logger.debug(f"End writing messages | {self._readable_id}")

    async def _read_messages(self, stdout: asyncio.StreamReader) -> None:
        logger.debug(f"Start reading messages | {self._readable_id}")
        content_length = 0

        try:
            while not self._stop_event.is_set():
                try:
                    header = await stdout.readline()
                except (ValueError, ConnectionResetError) as exc:
                    logger.warning(f"Read error | {self._readable_id}: {exc}")
                    break

                if not header:
                    if stdout.at_eof():
                        logger.debug(f"Reader reached EOF | {self._readable_id}")
                        break
                    continue

                if not content_length:
                    match = CONTENT_LENGTH_PATTERN.fullmatch(header)
                    if match:
                        content_length = int(match.group(1))
                    continue

                # Empty line after headers → read body
                if content_length and not header.strip():
                    try:
                        body = await stdout.readexactly(content_length)
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

                    try:
                        message = json.loads(body)
                    except json.JSONDecodeError as exc:
                        logger.error(f"JSON parse error | {self._readable_id}: {exc}")
                        continue

                    if not isinstance(message, dict):
                        logger.error(f"Expected dict message | {self._readable_id}")
                        continue

                    if self._on_message is not None:
                        try:
                            await self._on_message(message)
                        except Exception as exc:
                            logger.exception(
                                f"Error in message handler | {self._readable_id}: {exc}"
                            )
        except asyncio.CancelledError:
            pass

        logger.debug(f"End reading messages | {self._readable_id}")

    async def _log_stderr(self, stderr: asyncio.StreamReader) -> None:
        logger.debug(f"Start reading stderr | {self._readable_id}")
        try:
            while not self._stop_event.is_set():
                line = await stderr.readline()
                if not line:
                    break
                logger.debug(
                    f"Server stderr | {self._readable_id}: "
                    f"{line.decode('utf-8', errors='replace').rstrip()}"
                )
        except asyncio.CancelledError:
            pass
        logger.debug(f"End reading stderr | {self._readable_id}")

    async def _wait_for_exit(self) -> None:
        if self._process is None:
            return
        try:
            await self._process.wait()
        except asyncio.CancelledError:
            return

        logger.debug(
            f"Process exited with code {self._process.returncode} | {self._readable_id}"
        )

        if self._on_exit is not None:
            try:
                await self._on_exit()
            except Exception as exc:
                logger.exception(f"Error in exit handler | {self._readable_id}: {exc}")
