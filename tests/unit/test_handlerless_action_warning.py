"""Handlerless actions get one WARNING per action source, never per project.

A ~70-project workspace reports ~150 handlerless-action warning lines today —
two actions missing handlers in every project plus a handful more. Splitting
diagnostics by source keeps the project count in the WARNING and pushes the
project paths to DEBUG, so startup logs stay readable without hiding which
projects are affected.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from finecode.wm_server.runner import runner_manager


def test_handlerless_actions_warn_once_per_source() -> None:
    handlerless = {
        "fine_dist_artifacts.VerifyArtifactPublishedToRegistryAction": [
            Path("/ws/project-a"),
            Path("/ws/project-b"),
            Path("/ws/project-c"),
        ],
        "fine_tasks.CheckTasks": [
            Path("/ws/project-a"),
            Path("/ws/project-b"),
            Path("/ws/project-c"),
        ],
    }
    warnings: list[str] = []
    sink_id = logger.add(
        lambda m: warnings.append(m.record["message"]), level="WARNING"
    )
    try:
        runner_manager._warn_handlerless_actions(handlerless)
    finally:
        logger.remove(sink_id)

    assert len(warnings) == 2
    for source in sorted(handlerless):
        record = next(w for w in warnings if source in w)
        assert "3 project(s)" in record
        assert "project-a" not in record and "project-b" not in record