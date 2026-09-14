"""`AsyncProcess` streams a child's output line by line as it arrives.

The buffered path (`wait_for_end()` then `get_output()`) is unchanged, so both
are covered here: the streaming behaviour these tests pin down only earns its
place if it does not disturb the ~20 handlers that read output the old way.

Three of these are regressions for specific traps rather than API coverage:
a line longer than asyncio's default reader limit (which *discards* the buffer
before raising, so an undersized limit loses data silently), an undrained stderr
filling its pipe buffer and deadlocking the child, and a subscriber arriving
after output has already been produced.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from finecode_extension_api.interfaces import icommandrunner

from finecode_extension_runner.impls.command_runner import (
    AsyncProcess,
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.process_slots import ProcessSlots


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


def _runner() -> CommandRunner:
    return CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(),
        process_slots=ProcessSlots(target=4),
    )


def _python(script: str) -> str:
    # `shlex.quote`, not `repr`: `run()` goes through a shell, and `repr` would
    # turn the newlines in these scripts into literal backslash-n.
    return f"{sys.executable} -c {shlex.quote(script)}"


async def test_lines_arrive_before_the_process_exits() -> None:
    """The point of streaming: a caller sees output while the child still runs."""
    process = await _runner().run(
        _python(
            "import sys, time\n"
            "for i in range(3):\n"
            "    print(i, flush=True)\n"
            "    time.sleep(0.1)\n"
            "time.sleep(0.5)\n"
        )
    )

    received: list[str] = []
    async for line in process.stdout_lines():
        received.append(line)
        if len(received) == 3:
            # Still inside the child's final sleep -- if this only worked after
            # exit, we would not be here yet.
            assert process.get_exit_code() is None
            break

    assert received == ["0", "1", "2"]
    await process.wait_for_end()


async def test_line_longer_than_the_default_reader_limit_survives() -> None:
    """asyncio's default 64 KiB limit clears the buffer and raises `ValueError`.

    Line-framed JSON protocols exceed it routinely, so the limit is raised at
    spawn. Without that, this line would come back truncated or not at all.
    """
    payload_size = 512 * 1024
    process = await _runner().run(_python(f"print('x' * {payload_size}, flush=True)"))

    received = [line async for line in process.stdout_lines()]
    await process.wait_for_end()

    assert len(received) == 1
    assert received[0] == "x" * payload_size


async def test_unread_stderr_does_not_block_the_child() -> None:
    """Both streams are drained whether or not anyone subscribes.

    A caller streaming stdout and ignoring a chatty stderr is the shape that
    deadlocks if only the subscribed pipe is read: stderr's OS buffer fills and
    the child blocks writing to it, forever.
    """
    process = await _runner().run(
        _python(
            "import sys\n"
            "sys.stderr.write('e' * 1024 * 1024)\n"
            "sys.stderr.flush()\n"
            "print('done', flush=True)\n"
        )
    )

    received = [line async for line in process.stdout_lines()]
    await asyncio.wait_for(process.wait_for_end(), timeout=15)

    assert received == ["done"]
    assert process.get_exit_code() == 0
    # stderr was never subscribed, so it kept accumulating and is still readable.
    assert len(process.get_error_output()) == 1024 * 1024


async def test_subscribing_late_replays_what_was_already_produced() -> None:
    """Lines produced between spawn and subscribe are handed over, not dropped.

    This is the one way the stop-accumulating-on-subscribe rule could lose data,
    and for a line-framed protocol it would mean losing the opening messages.
    """
    process = await _runner().run(_python("print('first')\nprint('second')"))
    await process.wait_for_end()

    received = [line async for line in process.stdout_lines()]

    assert received == ["first", "second"]


async def test_buffered_output_is_unchanged_for_callers_that_never_subscribe() -> None:
    """The path every existing handler uses, including the trailing newline."""
    process = await _runner().run(
        _python("import sys\nprint('out')\nsys.stderr.write('err\\n')")
    )
    await process.wait_for_end()

    assert process.get_output() == "out\n"
    assert process.get_error_output() == "err\n"
    assert process.get_exit_code() == 0


async def test_output_without_a_trailing_newline_is_preserved() -> None:
    """Reassembling from lines must not invent a newline the child never wrote."""
    process = await _runner().run(_python("import sys\nsys.stdout.write('no-eol')"))
    await process.wait_for_end()

    assert process.get_output() == "no-eol"


async def test_get_output_after_subscribing_raises_rather_than_returning_empty() -> (
    None
):
    """Accumulation stops at subscribe, and says so.

    Returning `""` here would be indistinguishable from a child that printed
    nothing -- the silent-empty failure this design avoids everywhere else.
    """
    process = await _runner().run(_python("print('streamed')"))
    lines = process.stdout_lines()

    with pytest.raises(RuntimeError, match="consumed by a subscriber"):
        process.get_output()

    # stderr decided independently and still accumulates.
    assert process.get_error_output() == ""

    assert [line async for line in lines] == ["streamed"]
    await process.wait_for_end()


async def test_a_second_subscriber_to_one_stream_raises() -> None:
    process = await _runner().run(_python("print('once')"))
    process.stdout_lines()

    with pytest.raises(RuntimeError, match="already being consumed"):
        process.stdout_lines()

    await process.wait_for_end()


async def test_output_is_complete_when_wait_for_end_returns() -> None:
    """A process can exit with bytes still buffered in its pipes."""
    process = await _runner().run(_python("for i in range(500): print(i)"))
    await process.wait_for_end()

    assert process.get_output().splitlines() == [str(i) for i in range(500)]


async def test_undecodable_output_fails_the_subscriber_instead_of_ending_quietly() -> (
    None
):
    """A decode error must not look like a clean end of stream.

    `decode()` happens outside the guard around `readline`, so this is the easy
    way to get the exact failure the design forbids everywhere else: the
    subscriber's loop finishing normally on a stream that was actually cut off.
    """
    process = await _runner().run(
        _python(
            "import sys\n"
            "sys.stdout.buffer.write(b'good\\n')\n"
            "sys.stdout.buffer.write(b'\\xff\\xfe bad\\n')\n"
            "sys.stdout.buffer.write(b'after\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
    )

    received: list[str] = []
    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        async for line in process.stdout_lines():
            # A comprehension would be discarded when the iteration raises, and
            # what arrived before the failure is exactly what this test checks.
            received.append(line)  # noqa: PERF401

    assert received == ["good"]


async def test_stderr_is_complete_when_a_stdout_failure_surfaces() -> None:
    """The diagnostic must be readable at the moment the failure is reported.

    Reporting a stdout failure the instant it happens leaves stderr mid-drain,
    so the handler reaching for the child's error message finds an empty string
    -- precisely when it needs it most.
    """
    oversized = 9 * 1024 * 1024
    process = await _runner().run(
        _python(
            "import sys\n"
            f"sys.stdout.write('x' * {oversized})\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('the real error message\\n')\n"
            "sys.stderr.flush()\n"
        )
    )

    with pytest.raises(RuntimeError, match="exceeded"):
        await asyncio.wait_for(process.wait_for_end(), timeout=60)

    assert process.get_error_output() == "the real error message\n"


async def test_a_failed_stream_keeps_draining_so_the_child_still_exits() -> None:
    """Stopping a broken pump turns one bad line into a hung process.

    The child here writes far more than a pipe buffer holds after the line that
    overruns, so it can only reach its exit if the stream is still being read.
    """
    oversized = 9 * 1024 * 1024
    process = await _runner().run(
        _python(
            "import sys\n"
            f"sys.stdout.write('x' * {oversized})\n"
            "sys.stdout.write('\\n')\n"
            "for i in range(20000):\n"
            "    print('trailing line', i)\n"
            "sys.stdout.flush()\n"
            "sys.exit(3)\n"
        )
    )

    with pytest.raises(RuntimeError, match="exceeded"):
        await asyncio.wait_for(process.wait_for_end(), timeout=60)

    assert process.get_exit_code() == 3


async def test_abandoning_a_subscriber_stops_the_queue_from_growing() -> None:
    """A consumer that breaks out of the loop must not leave a queue filling.

    White-box on purpose: an unbounded queue nobody reads has no black-box
    signature short of running the process out of memory, and this is the exact
    buffer the class docstring claims streaming avoids.
    """
    process = await _runner().run(_python("for i in range(2000): print(i)"))
    assert isinstance(process, AsyncProcess)

    # `aclose()` rather than relying on the generator being collected: closing
    # is what a `break` eventually triggers, and doing it explicitly keeps the
    # assertion off the garbage collector's schedule.
    lines = cast("AsyncGenerator[str, None]", process.stdout_lines())
    received: list[str] = []
    async for line in lines:
        received.append(line)
        if len(received) == 2:
            break
    await lines.aclose()

    assert received == ["0", "1"]

    await process.wait_for_end()
    assert process._stdout._queue is None
    assert process._stdout._accumulated == []


async def test_a_drain_failure_nobody_awaits_is_not_logged_as_unhandled() -> None:
    """Streaming callers never call `wait_for_end()`, so nothing awaits the drain.

    `_log_traceback` is the flag asyncio's own `Future.__del__` consults before
    reporting "Task exception was never retrieved", which makes it the precise
    thing to assert -- the alternative is forcing a GC pass and scraping logs.
    """
    process = await _runner().run(
        _python(
            "import sys\n"
            "sys.stdout.buffer.write(b'\\xff\\xfe bad\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
    )
    assert isinstance(process, AsyncProcess)

    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        async for _ in process.stdout_lines():
            pass

    # Let the drain task finish on its own, as a streaming caller would.
    await asyncio.sleep(0.5)

    assert process._drain.done()
    assert process._drain._log_traceback is False


async def test_the_subscriber_and_the_drain_do_not_share_one_exception() -> None:
    """One instance raised from both splices their tracebacks together.

    The result reads as if the subscriber caused the drain to fail, which sends
    anyone debugging it to the wrong side of the pipe.
    """
    process = await _runner().run(
        _python(
            "import sys\n"
            "sys.stdout.buffer.write(b'\\xff\\xfe bad\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
    )

    with pytest.raises(RuntimeError) as from_subscriber:
        async for _ in process.stdout_lines():
            pass

    with pytest.raises(RuntimeError) as from_drain:
        await process.wait_for_end()

    assert from_subscriber.value is not from_drain.value
    assert isinstance(from_subscriber.value.__cause__, UnicodeDecodeError)


async def test_a_trailing_carriage_return_without_a_newline_is_data() -> None:
    """`readline` splits on `\\n` only, so a lone `\\r` never terminated a line.

    Tools that redraw progress emit exactly this, and stripping it both loses a
    byte and makes subscribers disagree with `text()`.
    """
    process = await _runner().run(
        _python("import sys\nsys.stdout.write('spin\\r')\nsys.stdout.flush()")
    )

    received = [line async for line in process.stdout_lines()]
    await process.wait_for_end()

    assert received == ["spin\r"]


async def test_crlf_line_endings_are_still_stripped() -> None:
    """The `\\r` that does precede a `\\n` is part of the terminator."""
    process = await _runner().run(
        _python(
            "import sys\nsys.stdout.write('one\\r\\ntwo\\r\\n')\nsys.stdout.flush()"
        )
    )

    received = [line async for line in process.stdout_lines()]
    await process.wait_for_end()

    assert received == ["one", "two"]


def test_the_streaming_methods_carry_their_contract_as_a_docstring() -> None:
    """A string after `...` in a Protocol body attaches to nothing.

    It looks like a docstring in the source and is invisible to `help()`, IDE
    hover and doc generators -- and these two methods are the only description
    of the single-subscriber and replay-on-late-subscribe rules.
    """
    assert icommandrunner.IAsyncProcess.stdout_lines.__doc__ is not None
    assert icommandrunner.IAsyncProcess.stderr_lines.__doc__ is not None
