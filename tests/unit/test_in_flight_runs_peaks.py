"""Project-activity peaks must survive after the runs drain."""

from __future__ import annotations

from pathlib import Path

from finecode.wm_server import context
from finecode.wm_server.services import in_flight_runs


async def test_overlapping_tracks_raise_projects_active_peak() -> None:
    """Two projects active at once must stay visible after both finish.

    Polling misses activity that starts and ends between samples; the peak is
    what shows an operator the burst actually happened.
    """
    ws_context = context.WorkspaceContext([])
    project_a = Path("/ws/a")
    project_b = Path("/ws/b")

    async with (
        in_flight_runs.track(
            ws_context,
            run_id="run-a",
            action_name="lint",
            project_path=project_a,
            origin=None,
        ),
        in_flight_runs.track(
            ws_context,
            run_id="run-b",
            action_name="test",
            project_path=project_b,
            origin=None,
        ),
    ):
        assert in_flight_runs.project_count(ws_context) == 2
        assert in_flight_runs.run_count(ws_context) == 2

    assert in_flight_runs.project_count(ws_context) == 0
    assert ws_context.resource_peaks.projects_active == 2
