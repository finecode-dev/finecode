"""ER stop cancels and awaits open owned scopes before closing the exit stack.

Why it matters: a scope held in a detached task must not outlive the runner —
otherwise the WM removes the env while a child is still running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import types
from collections.abc import Iterator

import pytest

from finecode_extension_runner import er_server
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
    _OwnedCommandScope,
)
from finecode_extension_runner.owned_commands import (
    get_owned_commands,
    reset_owned_commands,
)
from finecode_extension_runner.process_slots import ProcessSlots

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")

_IGNORE_TERM = ["sh", "-c", 'trap "" TERM; sleep 30 & wait']


@pytest.fixture(autouse=True)
def _forget_owned_commands() -> Iterator[None]:
    """Drop the process-wide registry after each test.

    These tests bind it to their own event loop (directly, or by starting an
    `ErServer`); a registry left bound to a closed loop refuses every owned
    scope a later test in the same process opens.
    """
    yield
    reset_owned_commands()


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


async def _wait_for_open(count: int, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while get_owned_commands().open_count < count:
        if loop.time() >= deadline:
            raise TimeoutError("scopes never opened")
        await asyncio.sleep(0.05)


async def _scopes(body_wait: float = 30.0) -> tuple[asyncio.Task[None], asyncio.Task[None], list[int]]:
    """Open one request-task scope and one detached-task scope; return pids."""
    slots = ProcessSlots(target=4)
    runner = CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(), process_slots=slots
    )
    pids: list[int] = []

    async def holder() -> None:
        async with _OwnedCommandScope(runner, _IGNORE_TERM, None, None, True) as proc:
            pids.append(proc.async_subprocess.pid)
            for _ in range(int(body_wait * 20)):
                if proc.group_survivors() and len(proc.group_survivors()) >= 2:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(body_wait)

    request_task: asyncio.Task[None] = asyncio.ensure_future(holder())
    detached_task: asyncio.Task[None] = asyncio.ensure_future(holder())
    await _wait_for_open(2)
    return request_task, detached_task, pids


def _assert_all_gone(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"child {pid} survived ER stop")


async def test_stdio_stop_cancels_and_awaits_open_scopes() -> None:
    """Graceful stdio stop ends both scopes before the exit stack closes."""
    reset_owned_commands()
    server = er_server.ErServer()
    observed: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def _exit_stack_probe():  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            observed["open"] = get_owned_commands().open_count
            observed["pids"] = list(_pids)

    stdin_read, stdin_write = os.pipe()
    stdin_buf = os.fdopen(stdin_read, "rb")
    stdout_buf = open(os.devnull, "wb")  # noqa: ASYNC230, SIM115 - the transport requires a real fd
    server_task = asyncio.create_task(
        server.start_io_async(stdin_buf=stdin_buf, stdout_buf=stdout_buf)
    )
    request_task: asyncio.Task[None] | None = None
    detached_task: asyncio.Task[None] | None = None
    _pids: list[int] = []
    try:
        await asyncio.sleep(0.5)
        request_task, detached_task, _pids = await _scopes()
        await server._finecode_exit_stack.enter_async_context(_exit_stack_probe())
        transport = server._session._transport
        assert transport is not None
        loop = asyncio.get_running_loop()
        start = loop.time()
        await transport.stop()
        await asyncio.wait_for(server_task, timeout=15)
        assert loop.time() - start < 10.0
    finally:
        if not server_task.done():
            _ = server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
        os.close(stdin_write)
        stdin_buf.close()
        stdout_buf.close()
    assert request_task is not None and request_task.done()
    assert detached_task is not None and detached_task.done()
    assert observed.get("open") == 0
    _assert_all_gone(_pids)


async def test_tcp_stop_cancels_and_awaits_open_scopes() -> None:
    """Graceful TCP stop ends both scopes before the exit stack closes."""
    reset_owned_commands()
    server = er_server.ErServer()
    observed: dict[str, object] = {}
    request_task: asyncio.Task[None] | None = None
    detached_task: asyncio.Task[None] | None = None
    _pids: list[int] = []

    @contextlib.asynccontextmanager
    async def _exit_stack_probe():  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            observed["open"] = get_owned_commands().open_count

    server_task = asyncio.create_task(server._run_tcp("127.0.0.1", 0))
    try:
        for _ in range(100):
            if server._tcp_server is not None and server._tcp_server.sockets:
                break
            await asyncio.sleep(0.05)
        assert server._tcp_server is not None and server._tcp_server.sockets
        port = server._tcp_server.sockets[0].getsockname()[1]
        request_task, detached_task, _pids = await _scopes()
        await server._finecode_exit_stack.enter_async_context(_exit_stack_probe())
        loop = asyncio.get_running_loop()
        start = loop.time()
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server_task, timeout=15)
        assert loop.time() - start < 10.0
    finally:
        if not server_task.done():
            _ = server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
    assert request_task is not None and request_task.done()
    assert detached_task is not None and detached_task.done()
    assert observed.get("open") == 0
    _assert_all_gone(_pids)


async def test_scope_cleanup_survives_loop_close(tmp_path) -> None:
    """A scope mid-cleanup under asyncio.run is SIGKILLed before the loop closes."""
    script = tmp_path / "loop_close.py"
    script.write_text(
        "import asyncio, sys\n"
        "from finecode_extension_runner.impls.command_runner import (\n"
        "    CommandRunner, CommandRunnerConfig, _OwnedCommandScope,\n"
        ")\n"
        "from finecode_extension_runner.owned_commands import get_owned_commands\n"
        "from finecode_extension_runner.process_slots import ProcessSlots\n"
        "class L:\n"
        "    def debug(self, m): pass\n"
        "    def trace(self, m): pass\n"
        "    def info(self, m): pass\n"
        "    def warning(self, m): pass\n"
        "    def error(self, m): pass\n"
        "    def exception(self, e): pass\n"
        "    def disable(self, p): pass\n"
        "    def enable(self, p): pass\n"
        "async def main():\n"
        "    get_owned_commands().bind_loop(asyncio.get_running_loop())\n"
        "    runner = CommandRunner(logger=L(), config=CommandRunnerConfig(),\n"
        "                           process_slots=ProcessSlots(target=4))\n"
        "    async def holder():\n"
        "        async with _OwnedCommandScope(runner, ['sh', '-c', 'trap \"\" TERM; sleep 30 & wait'], None, None, True) as proc:\n"
        "            print(f'PID={proc.async_subprocess.pid}', flush=True)\n"
        "            await asyncio.sleep(30)\n"
        "    task = asyncio.ensure_future(holder())\n"
        "    await asyncio.sleep(0.5)\n"
        "    task.cancel()\n"
        "    await asyncio.sleep(0.5)\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 0
    pid = int([line for line in out.decode().splitlines() if line.startswith("PID=")][0][4:])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"child {pid} survived loop close")


async def test_retire_runner_context_keeps_open_scope() -> None:
    """Retiring the context does not end an open scope; it exits normally."""
    from finecode_extension_runner.owned_commands import OwnedCommandRegistry

    reset_owned_commands()
    registry = get_owned_commands()
    registry.bind_loop(asyncio.get_running_loop())
    slots = ProcessSlots(target=4)
    di_registry = Registry()
    di_registry.register_instance(OwnedCommandRegistry, registry)
    di_registry.register_instance(ProcessSlots, slots)
    server = er_server.ErServer()
    previous = types.SimpleNamespace(action_cache_by_name={}, di_registry=di_registry)
    runner = CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(), process_slots=slots
    )
    done: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def holder() -> None:
        async with _OwnedCommandScope(
            runner, [sys.executable, "-c", "import time; time.sleep(0.5); print('ok')"], None, None, True
        ) as proc:
            await proc.wait_for_end()
            if not done.done():
                done.set_result(proc.get_output())

    task: asyncio.Task[None] = asyncio.ensure_future(holder())
    await _wait_for_open(1)
    entry_pid = next(iter(registry._entries.values())).pid
    assert entry_pid is not None
    await er_server._retire_runner_context(server, previous)
    assert get_owned_commands() is registry
    try:
        os.kill(entry_pid, 0)
    except ProcessLookupError:
        raise AssertionError("scope process died at context retirement")
    assert await asyncio.wait_for(done, timeout=10.0) == "ok\n"
    await asyncio.wait_for(task, timeout=10.0)
    assert slots.in_flight == 0
