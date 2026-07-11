from __future__ import annotations

import typing

import pytest

from finecode.wm_server import domain
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner.runner_client import RunActionResponse
from finecode.wm_server.services.run_service import matrix_runner
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed


def _make_matrix_action(*, interpreters: list[str]) -> domain.Action:
    handlers = [
        domain.ActionHandler(
            name=f"handler_{interpreter}",
            source="test.handlers.TestHandler",
            config={},
            env=f"testing@{interpreter}",
            dependencies=[],
            interpreter=interpreter,
        )
        for interpreter in interpreters
    ]
    return domain.Action(
        name="test_action",
        source="test.actions.TestAction",
        handlers=handlers,
        config={},
    )


def test_combine_variant_responses_nests_json_and_text_by_interpreter() -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")

    variants = {
        cpython_311: RunActionResponse(
            result_by_format={"json": {"ok": True}, "string": "all good on 3.11"},
            return_code=0,
        ),
        cpython_312: RunActionResponse(
            result_by_format={"json": {"ok": False}, "string": "problem on 3.12"},
            return_code=1,
        ),
    }

    combined = matrix_runner._combine_variant_responses(variants)

    assert combined.result_by_format["json"] == {
        "cpython@3.11": {"ok": True},
        "cpython@3.12": {"ok": False},
    }
    assert "=== cpython@3.11 ===" in combined.result_by_format["string"]
    assert "=== cpython@3.12 ===" in combined.result_by_format["string"]
    assert "all good on 3.11" in combined.result_by_format["string"]
    assert "problem on 3.12" in combined.result_by_format["string"]
    assert combined.return_code == 1


def test_combine_variant_responses_ors_return_code() -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")

    variants = {
        cpython_311: RunActionResponse(result_by_format={"json": {}}, return_code=0),
        cpython_312: RunActionResponse(result_by_format={"json": {}}, return_code=1),
    }

    combined = matrix_runner._combine_variant_responses(variants)

    assert combined.return_code == 1


async def test_run_matrix_action_invokes_run_variant_once_per_interpreter_with_filtered_handlers() -> None:
    action = _make_matrix_action(interpreters=["cpython@3.11", "cpython@3.12"])

    calls: list[dict[str, typing.Any]] = []

    async def fake_run_variant(**kwargs: typing.Any) -> RunActionResponse:
        calls.append(kwargs)
        variant_action: domain.Action = kwargs["action"]
        env_names = {handler.env for handler in variant_action.handlers}
        return RunActionResponse(
            result_by_format={"json": {"envs": sorted(env_names)}},
            return_code=0,
        )

    response = await matrix_runner.run_matrix_action(
        action=action,
        action_name="test_action",
        payload={},
        project_def=None,
        ws_context=None,
        run_trigger=None,
        dev_env=None,
        result_formats=[],
        initialize_all_handlers=False,
        progress_token=None,
        wal_run_id="wal-1",
        traceparent=None,
        orchestration_depth=0,
        caller_kwargs=None,
        run_variant=fake_run_variant,
    )

    assert len(calls) == 2
    called_envs = {
        frozenset(handler.env for handler in call["action"].handlers) for call in calls
    }
    assert called_envs == {
        frozenset({"testing@cpython@3.11"}),
        frozenset({"testing@cpython@3.12"}),
    }

    assert response.result_by_format["json"] == {
        "cpython@3.11": {"envs": ["testing@cpython@3.11"]},
        "cpython@3.12": {"envs": ["testing@cpython@3.12"]},
    }
    assert response.return_code == 0


async def test_run_matrix_action_isolates_variant_failure() -> None:
    action = _make_matrix_action(interpreters=["cpython@3.11", "cpython@3.12"])

    async def flaky_run_variant(**kwargs: typing.Any) -> RunActionResponse:
        variant_action: domain.Action = kwargs["action"]
        if any(handler.interpreter == "cpython@3.12" for handler in variant_action.handlers):
            raise RuntimeError("boom on 3.12")
        return RunActionResponse(result_by_format={"json": {"ok": True}}, return_code=0)

    response = await matrix_runner.run_matrix_action(
        action=action,
        action_name="test_action",
        payload={},
        project_def=None,
        ws_context=None,
        run_trigger=None,
        dev_env=None,
        result_formats=[],
        initialize_all_handlers=False,
        progress_token=None,
        wal_run_id="wal-1",
        traceparent=None,
        orchestration_depth=0,
        caller_kwargs=None,
        run_variant=flaky_run_variant,
    )

    assert response.return_code != 0
    result_json = response.result_by_format["json"]
    assert result_json["cpython@3.11"] == {"ok": True}
    assert "error" in result_json["cpython@3.12"]


async def test_run_matrix_action_with_selected_interpreters_runs_only_that_variant() -> None:
    action = _make_matrix_action(interpreters=["cpython@3.11", "cpython@3.12"])

    calls: list[dict[str, typing.Any]] = []

    async def fake_run_variant(**kwargs: typing.Any) -> RunActionResponse:
        calls.append(kwargs)
        return RunActionResponse(result_by_format={"json": {"ok": True}}, return_code=0)

    response = await matrix_runner.run_matrix_action(
        action=action,
        action_name="test_action",
        payload={},
        project_def=None,
        ws_context=None,
        run_trigger=None,
        dev_env=None,
        result_formats=[],
        initialize_all_handlers=False,
        progress_token=None,
        wal_run_id="wal-1",
        traceparent=None,
        orchestration_depth=0,
        caller_kwargs=None,
        run_variant=fake_run_variant,
        selected_interpreters={"cpython@3.11"},
    )

    assert len(calls) == 1
    called_action: domain.Action = calls[0]["action"]
    assert {handler.interpreter for handler in called_action.handlers} == {"cpython@3.11"}
    assert set(response.result_by_format["json"].keys()) == {"cpython@3.11"}


async def test_run_matrix_action_with_unknown_selected_interpreter_raises() -> None:
    action = _make_matrix_action(interpreters=["cpython@3.11", "cpython@3.12"])

    async def fake_run_variant(**kwargs: typing.Any) -> RunActionResponse:
        return RunActionResponse(result_by_format={"json": {"ok": True}}, return_code=0)

    with pytest.raises(ActionRunFailed):
        await matrix_runner.run_matrix_action(
            action=action,
            action_name="test_action",
            payload={},
            project_def=None,
            ws_context=None,
            run_trigger=None,
            dev_env=None,
            result_formats=[],
            initialize_all_handlers=False,
            progress_token=None,
            wal_run_id="wal-1",
            traceparent=None,
            orchestration_depth=0,
            caller_kwargs=None,
            run_variant=fake_run_variant,
            selected_interpreters={"cpython@3.14"},
        )
