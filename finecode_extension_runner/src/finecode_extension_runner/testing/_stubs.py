from __future__ import annotations

import dataclasses
import typing

from finecode_extension_api.interfaces import ilogger, iuserprompt

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


class FakeUserPrompt:
    """IUserPrompt that answers from a script, and records what it was asked.

    The default answer is ``UNAVAILABLE`` — the same thing a handler gets in CI,
    in a pipeline, and from any client that did not declare the capability. That
    is the path most likely to be wrong and least likely to be written a test
    for, so it is what a handler under test meets unless the test says otherwise
    (ADR-0082 rule 3).

    Scripted answers are consumed in order, one per ``ask_choice``; running out
    falls back to the default rather than raising, so a test that adds a second
    question does not fail in the fixture instead of in the assertion.
    """

    def __init__(
        self,
        answers: list[iuserprompt.ElicitationResult] | None = None,
        default: iuserprompt.ElicitationResult | None = None,
    ) -> None:
        self.answers = list(answers or [])
        self.default = default or iuserprompt.ElicitationResult(
            outcome=iuserprompt.ElicitationOutcome.UNAVAILABLE
        )
        self.asked: list[tuple[str, list[str]]] = []

    async def ask_choice(
        self,
        message: str,
        options: list[str],
        *,
        default: str | None = None,
        timeout_sec: float = 300.0,
    ) -> iuserprompt.ElicitationResult:
        self.asked.append((message, list(options)))
        if self.answers:
            return self.answers.pop(0)
        return self.default
