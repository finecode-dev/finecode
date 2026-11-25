"""Registry of open owned-command scopes for one ER process.

The ER cancels and awaits every open scope on graceful stop, so a scope held
in a detached task cannot outlive the runner and keep a child alive past env
removal. Like :mod:`process_slots`, this is an ER-lifetime module singleton,
independent of any ``RunnerContext`` rebuild, and it sits outside the
``er-layered`` stack: it imports neither ``er_server`` nor the layer packages.
"""

from __future__ import annotations

import asyncio
import dataclasses

from loguru import logger

__all__ = [
    "OwnedCommandEntry",
    "OwnedCommandRegistry",
    "get_owned_commands",
    "reset_owned_commands",
    "set_owned_commands",
]


@dataclasses.dataclass
class OwnedCommandEntry:
    holder: asyncio.Task[object]
    done: asyncio.Future[None]
    pid: int | None = None


class OwnedCommandRegistry:
    """Tracks open scopes by holder task, plus background work to keep alive."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._entries: dict[asyncio.Task[object], OwnedCommandEntry] = {}
        self._kept: set[asyncio.Task[None]] = set()
        self._kept_descriptions: dict[asyncio.Task[None], str] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def check_loop(self) -> None:
        """Raise unless the scope opens on the ER's event loop."""
        if self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is not self._loop:
            raise RuntimeError("Owned command scopes may only open on the ER event loop")

    def register(self, holder: asyncio.Task[object]) -> OwnedCommandEntry:
        entry = OwnedCommandEntry(holder=holder, done=holder.get_loop().create_future())
        self._entries[holder] = entry
        return entry

    def complete(self, entry: OwnedCommandEntry) -> None:
        _ = self._entries.pop(entry.holder, None)
        if not entry.done.done():
            entry.done.set_result(None)

    def keep(self, task: asyncio.Task[None], description: str) -> None:
        """Hold a strong reference to background work (watchers, releases)."""
        self._kept.add(task)
        self._kept_descriptions[task] = description

        def _drop(done: asyncio.Task[None]) -> None:
            self._kept.discard(done)
            _ = self._kept_descriptions.pop(done, None)

        task.add_done_callback(_drop)

    @property
    def open_count(self) -> int:
        return len(self._entries)

    async def close_all(self) -> None:
        """Cancel every open holder and await each entry's done, unbounded."""
        entries = list(self._entries.values())
        for entry in entries:
            if not entry.holder.done():
                _ = entry.holder.cancel()
        for entry in entries:
            try:
                await entry.done
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a holder may end with anything
                logger.debug(f"Owned command holder ended with {error!r}")
        for task in list(self._kept):
            if not task.done():
                logger.warning(
                    "Owned command background work still pending at ER stop: "
                    f"{self._kept_descriptions.get(task, '?')}"
                )


_owned_commands: OwnedCommandRegistry | None = None


def get_owned_commands() -> OwnedCommandRegistry:
    """The process-wide registry, created lazily."""
    global _owned_commands
    if _owned_commands is None:
        _owned_commands = OwnedCommandRegistry()
    return _owned_commands


def set_owned_commands(registry: OwnedCommandRegistry) -> None:
    """Replace the process-wide registry. Tests only."""
    global _owned_commands
    _owned_commands = registry


def reset_owned_commands() -> None:
    """Forget the process-wide registry. Tests only."""
    global _owned_commands
    _owned_commands = None
