"""Slot through which whoever owns client connections serves the runner layer.

An ER pushes log records and user messages to the runner's JSON-RPC client, and the
runner learns when a client subscribes to log forwarding — but broadcasting to
connected clients and tracking subscriptions is owned by ``wm_server.py``, which sits
*above* the runner in the WM's layer stack. See ADR-0072 for why this is a slot the
owner fills on import rather than an upward import.

Unlike the other slots, an unfilled one here is not an error: these are best-effort
notifications, so the default implementation drops them (see ``_NoopBridge``).
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from finecode.wm_server.runner.runner_client import ExtensionRunnerInfo

__all__ = ["ClientBridge", "handlers", "install", "reset"]


class ClientBridge(typing.Protocol):
    """What the runner needs from whoever owns client connections."""

    def notify_all_clients(self, method: str, params: dict[str, typing.Any]) -> None:
        """Broadcast a JSON-RPC notification to every connected client."""

    def deliver_er_log_record(
        self, *, source: str, timestamp: float, level: str, group: str, message: str
    ) -> None:
        """Redact and forward one ER log record into the client log-delivery pipeline."""

    async def push_er_forwarding_to_runner(self, runner: ExtensionRunnerInfo) -> None:
        """Send ``updateLogging`` to this runner if a client's subscription state
        means its desired forwarding state changed. Best-effort."""


class _NoopBridge:
    """Drops everything. The default, in force whenever no owner has installed itself.

    A WM assembled without its client-connection layer — a runner exercised
    standalone in a test — has nobody to notify, which is a real state and not a
    defect. Every operation on this bridge is a notification the sender does not
    wait on, so dropping it is the correct answer rather than a swallowed failure;
    that is what makes a null object right here and wrong for the slots whose
    callers need an answer back.
    """

    def notify_all_clients(self, method: str, params: dict[str, typing.Any]) -> None:
        pass

    def deliver_er_log_record(
        self, *, source: str, timestamp: float, level: str, group: str, message: str
    ) -> None:
        pass

    async def push_er_forwarding_to_runner(self, runner: ExtensionRunnerInfo) -> None:
        pass


_installed: ClientBridge = _NoopBridge()


def install(implementation: ClientBridge) -> None:
    """Nominate *implementation* as the answer to client-connection requests from a runner."""
    global _installed
    _installed = implementation


def reset() -> None:
    """Restore the drop-everything default. Tests only."""
    global _installed
    _installed = _NoopBridge()


def handlers() -> ClientBridge:
    """The installed implementation, or the drop-everything default if none was."""
    return _installed
