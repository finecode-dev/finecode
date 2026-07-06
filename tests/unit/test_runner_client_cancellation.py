from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client


async def test_run_action_raises_action_run_cancelled_on_er_cancellation(
    tmp_path: pathlib.Path,
) -> None:
    """When the ER signals a genuine JSON-RPC cancellation (code ==
    REQUEST_CANCELLED) for ``actions/run``, ``runner_client.run_action`` must
    raise ``ActionRunCancelled`` — a distinct, benign outcome from an ordinary
    ``ActionRunFailed`` — so callers can propagate cancellation instead of
    logging it as a failure.
    """
    client = wm_testing.FakeErClient()
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(runner_client.ActionRunCancelled) as exc_info:
        await runner_client.run_action(runner=runner, action_name="test_action", params={})

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_action_unrelated_error_code_propagates_unwrapped(
    tmp_path: pathlib.Path,
) -> None:
    """An ``actions/run`` error with an unrelated JSON-RPC code must not be
    mistranslated into a cancellation — the original transport exception
    propagates unchanged.
    """
    client = wm_testing.FakeErClient()
    original = wm_testing.make_error_on_request(-32603, "boom")
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(type(original)) as exc_info:
        await runner_client.run_action(runner=runner, action_name="test_action", params={})

    assert exc_info.value is original


async def test_run_handlers_raises_action_run_cancelled_on_er_cancellation(
    tmp_path: pathlib.Path,
) -> None:
    """Same cancellation propagation contract as ``run_action``, but for the
    ``actions/runHandlers`` multi-env segment call.
    """
    client = wm_testing.FakeErClient()
    client.configure_error(wm_testing.make_cancelled_error("cancelled by pyrefly"))
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(runner_client.ActionRunCancelled) as exc_info:
        await runner_client.run_handlers(
            runner=runner,
            action_name="test_action",
            handler_names=["h1"],
        )

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_run_handlers_unrelated_error_code_propagates_unwrapped(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard mirroring ``run_action``'s: an unrelated error code on
    ``actions/runHandlers`` must not be reclassified as a cancellation.
    """
    client = wm_testing.FakeErClient()
    original = wm_testing.make_error_on_request(-32603, "boom")
    client.configure_error(original)
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)

    with pytest.raises(type(original)) as exc_info:
        await runner_client.run_handlers(
            runner=runner,
            action_name="test_action",
            handler_names=["h1"],
        )

    assert exc_info.value is original
