"""Spawn FineCode-owned server processes from an argv sequence.

A server is exec'd directly from argv with no shell between the caller and the
process, so a path containing spaces or shell metacharacters cannot be re-parsed
and no ``cmd.exe`` sits in the middle on Windows. On Windows the only creation
flag is ``CREATE_NO_WINDOW``: Microsoft documents it as ignored when combined
with ``DETACHED_PROCESS``, so adding the latter silently disables the hidden
console this intends.
"""

from __future__ import annotations

import asyncio
import collections.abc
import sys
import typing
from pathlib import Path

__all__ = ["SpawnCommand", "spawn_process"]

SpawnCommand = collections.abc.Sequence[str]

# Win32 CREATE_NO_WINDOW (processthreadsapi). Spelled as a literal because
# `subprocess.CREATE_NO_WINDOW` only exists on Windows, and the flag choice is
# unit-tested on every platform.
_CREATE_NO_WINDOW = 0x08000000


def _platform_spawn_kwargs(platform: str) -> dict[str, typing.Any]:
    if platform == "win32":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {"start_new_session": True}


async def spawn_process(
    cmd: SpawnCommand,
    *,
    stdin_pipe: bool,
    cwd: Path | None,
    env: dict[str, str] | None,
    limit: int | None = None,
) -> asyncio.subprocess.Process:
    """Spawn *cmd* as a child process, executing it directly.

    Raises:
        TypeError: *cmd* is a ``str``. A ``str`` satisfies ``Sequence[str]``, so
            the type checker cannot reject one, and exec'ing ``"ruff server"``
            would run argv ``['r', 'u', 'f', ...]`` instead of the server.
        ValueError: *cmd* is empty.
    """
    if isinstance(cmd, str):
        raise TypeError("cmd must be an argv sequence, not a str")
    argv = list(cmd)
    if not argv:
        raise ValueError("empty command")

    kwargs = _platform_spawn_kwargs(sys.platform)
    if limit is not None:
        kwargs["limit"] = limit

    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_pipe else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **kwargs,
    )
