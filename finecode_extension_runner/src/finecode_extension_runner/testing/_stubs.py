from __future__ import annotations

import dataclasses
import typing

from finecode_extension_api.interfaces import ilogger
from finecode_extension_runner import er_wal


@dataclasses.dataclass
class WalEvent:
    event_type: str
    wal_run_id: str
    action_name: str
    project_path: str
    trigger: str
    dev_env: str
    payload: dict[str, typing.Any]


class NullWalWriter(er_wal.ErWalWriter):
    """WAL writer that silently drops all events. Use when WAL assertions are not needed."""

    def __init__(self) -> None:
        pass

    def append(
        self,
        *,
        event_type: er_wal.ErWalEventType | str,
        wal_run_id: str,
        action_name: str,
        project_path: str,
        trigger: str,
        dev_env: str,
        payload: typing.Any | None = None,
    ) -> None:
        pass

    def close(self) -> None:
        pass


class InMemoryWalWriter(er_wal.ErWalWriter):
    """WAL writer that records events in memory for assertion in tests.

    Access via ``writer.events`` or filter by type:
        rfa = [e for e in writer.events if e.event_type == ErWalEventType.RUN_COMPLETED]
    """

    def __init__(self) -> None:
        self.events: list[WalEvent] = []

    def append(
        self,
        *,
        event_type: er_wal.ErWalEventType | str,
        wal_run_id: str,
        action_name: str,
        project_path: str,
        trigger: str,
        dev_env: str,
        payload: typing.Any | None = None,
    ) -> None:
        self.events.append(
            WalEvent(
                event_type=(
                    event_type.value
                    if isinstance(event_type, er_wal.ErWalEventType)
                    else event_type
                ),
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=project_path,
                trigger=trigger,
                dev_env=dev_env,
                payload=er_wal._serialize_payload(payload),
            )
        )

    def close(self) -> None:
        pass


class NoOpLogger(ilogger.ILogger):
    """ILogger implementation that silently discards all log messages."""

    def exception(self, exception: Exception) -> None:
        pass

    def trace(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def disable(self, package: str) -> None:
        pass

    def enable(self, package: str) -> None:
        pass


class FakeUserMessenger:
    """IUserMessenger that records sent messages for assertion in tests."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)
