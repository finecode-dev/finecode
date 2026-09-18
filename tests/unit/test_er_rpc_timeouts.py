"""Requirement tests: which ER RPCs carry a timeout, and which deliberately do not.

REQUIREMENT: a control-plane RPC — shutdown, config/logging/budget updates,
schema and package introspection — can never legitimately take long, so it must
be bounded. Otherwise a channel that stops answering parks the calling
workspace operation forever, with no log and no error.

Work RPCs (``run_action``/``run_handlers``/``merge_results``) are the opposite:
a handler may block indefinitely by design (a server that runs until cancelled,
or ``prepare-envs`` running for hours). They must stay unbounded and rely on
channel-death propagation to fail them when the transport closes.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_api, runner_client


class _NoResponse(Exception):
    """Stops the RPC at the fake client; stands in for the transport."""


class _RecordingErClient:
    """Fake ER client that records the ``timeout`` passed per request."""

    def __init__(self) -> None:
        self.readable_id = "recording-er"
        self.calls: list[tuple[str, float | None]] = []

    async def send_request(
        self,
        method: str,
        params: object = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append((method, timeout))
        raise _NoResponse()


async def test_control_plane_rpcs_are_bounded(tmp_path: pathlib.Path) -> None:
    """Every control-plane RPC passes a finite positive timeout."""
    client = _RecordingErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    calls = [
        _internal_client_api.shutdown(client),
        runner_client.update_config(
            runner,
            tmp_path / "pyproject.toml",
            runner_client.RunnerConfig(actions=[], action_handler_configs={}),
        ),
        runner_client.update_logging(runner, True, "INFO"),
        runner_client.update_process_budget(runner, 4),
        runner_client.reload_action(runner, "some_action"),
        runner_client.resolve_action_meta(runner),
        runner_client.get_payload_schemas(runner),
        runner_client.resolve_package_path(runner, "some_package"),
    ]

    for call in calls:
        with pytest.raises(_NoResponse):
            await call

    assert len(client.calls) == len(calls)
    for method, timeout in client.calls:
        assert timeout is not None, f"{method} has no timeout"
        assert timeout > 0, f"{method} has non-positive timeout {timeout}"


async def test_work_rpcs_remain_unbounded_by_design(tmp_path: pathlib.Path) -> None:
    """Work RPCs stay unbounded so legitimately long handlers are not aborted."""
    client = _RecordingErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    calls = [
        runner_client.run_action(runner, "some_action", {}),
        runner_client.run_handlers(runner, "some_action", ["some_handler"]),
        runner_client.merge_results(runner, "some_action", []),
    ]

    for call in calls:
        with pytest.raises(_NoResponse):
            await call

    assert len(client.calls) == len(calls)
    for method, timeout in client.calls:
        assert timeout is None, f"{method} unexpectedly has timeout {timeout}"
