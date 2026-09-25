"""Requirement tests (criterion 2 and 3) for the shared spawn helper.

REQUIREMENT: every FineCode-owned server spawn must execute an argv sequence
directly, with no shell in between. A ``str`` that slips through would be
exec'd character-by-character; an empty command would raise an opaque error deep
inside the event loop. The Windows creation flags must carry
``CREATE_NO_WINDOW`` alone, because Windows ignores it when combined with
``DETACHED_PROCESS``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from finecode_jsonrpc import _spawn, _spawn_selfcheck


async def test_spawn_command_form(monkeypatch) -> None:
    """A list/tuple is unpacked into argv; a str and an empty sequence are
    rejected before anything is spawned."""
    calls: list[tuple[tuple, dict]] = []

    async def _recorder(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recorder)

    await _spawn.spawn_process(
        ["some-server", "--stdio"], stdin_pipe=True, cwd=None, env=None
    )
    assert calls[0][0] == ("some-server", "--stdio")

    await _spawn.spawn_process(
        ("other-server", "arg"), stdin_pipe=False, cwd=Path("."), env={}
    )
    assert calls[1][0] == ("other-server", "arg")

    with pytest.raises(TypeError):
        await _spawn.spawn_process("a b", stdin_pipe=True, cwd=None, env=None)
    with pytest.raises(ValueError):
        await _spawn.spawn_process([], stdin_pipe=True, cwd=None, env=None)

    assert len(calls) == 2


def test_platform_spawn_kwargs_windows_no_detach() -> None:
    """CREATE_NO_WINDOW alone: Windows ignores it when DETACHED_PROCESS is set."""
    kwargs = _spawn._platform_spawn_kwargs("win32")

    assert kwargs == {"creationflags": 0x08000000}
    assert kwargs["creationflags"] & 0x00000008 == 0


def test_platform_spawn_kwargs_posix_new_session() -> None:
    assert _spawn._platform_spawn_kwargs("linux") == {"start_new_session": True}


async def test_tcp_real_spawn() -> None:
    """A TCP server spawned through the real client records ITS pid, not a
    wrapper's. A shell between the client and the server would make the two pids
    disagree, which is exactly the failure that left the ER alive with nothing
    on its pipes on Windows."""
    pid, stdout_lines = await _spawn_selfcheck.check_tcp(
        _spawn_selfcheck.argv_for(_spawn_selfcheck._FAKE_TCP_SERVER),
        timeout=20,
    )

    reported = next(
        (
            int(line.split("=", 1)[1])
            for line in stdout_lines
            if line.startswith("pid=")
        ),
        None,
    )
    assert reported is not None, "fake server did not report its pid"
    assert pid == reported


async def test_stdio_real_spawn() -> None:
    """An STDIO server spawned through the real transport answers one frame.
    A spawn regression shows up here as a 10s timeout instead of a silent gap."""
    await _spawn_selfcheck.check_stdio(
        _spawn_selfcheck.argv_for(_spawn_selfcheck._FAKE_STDIO_SERVER),
        timeout=10,
    )
