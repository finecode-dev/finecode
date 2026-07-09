"""ADR-0049 — integration tests for the ER -> WM log record receive path.

Drives ``runner_manager.handle_er_log_records`` directly (no real ER, no
subprocess), then asserts the record reaches a subscribed ``InProcClient``
over the real WM dispatch loop (see ``tests/integration/conftest.py``).
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from finecode.wm_server.runner import runner_manager


@dataclasses.dataclass
class _FakeRunner:
    """Lightweight stand-in for ``ExtensionRunnerInfo`` -- enough for the
    ``source`` tag built by ``handle_er_log_records``."""

    env_name: str
    working_dir_path: Path


def _fake_runner() -> _FakeRunner:
    return _FakeRunner(env_name="test_env", working_dir_path=Path("/tmp/some_project"))


async def test_er_log_record_reaches_subscribed_client(wm_client) -> None:
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    runner = _fake_runner()
    runner_manager.handle_er_log_records(
        runner,
        {"records": [{"timestamp": 0.0, "level": "INFO", "group": "ext.x", "message": f"boot ok-{m}"}]},
    )

    params = await wm_client.next_notification("server/logRecords")
    records = params.get("records", [])
    matching = [r for r in records if r["message"] == f"boot ok-{m}"]
    assert matching, records
    assert matching[0]["source"] == "runner:test_env@some_project"


async def test_er_log_record_redacted_at_wm_boundary(wm_client) -> None:
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    runner = _fake_runner()
    runner_manager.handle_er_log_records(
        runner,
        {
            "records": [
                {
                    "timestamp": 0.0,
                    "level": "INFO",
                    "group": "ext.x",
                    "message": f"token=abc123 done-{m}",
                }
            ]
        },
    )

    params = await wm_client.next_notification("server/logRecords")
    messages = [r["message"] for r in params.get("records", [])]
    assert f"token=***REDACTED*** done-{m}" in messages


async def test_er_log_records_not_delivered_when_unobserved(wm_client) -> None:
    """Without any subscriber, handle_er_log_records delivers nothing (Phase-1
    _deliver_record fast-path on an empty registry)."""
    from finecode.wm_server import wm_server

    runner = _fake_runner()
    runner_manager.handle_er_log_records(
        runner,
        {"records": [{"timestamp": 0.0, "level": "INFO", "group": "ext.x", "message": "unseen"}]},
    )

    assert wm_server._log_registry.has_subscribers() is False
