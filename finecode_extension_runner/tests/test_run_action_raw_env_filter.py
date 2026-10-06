from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from finecode_extension_api import code_action

from finecode_extension_runner import global_state, schemas
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _LabelsRunResult(code_action.RunActionResult):
    labels: list[str] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _LabelsRunResult):
            return
        self.labels.extend(other.labels)


class _LabelsTestAction(
    code_action.Action[
        code_action.RunActionPayload,
        code_action.RunActionContext,
        _LabelsRunResult,
    ]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = _LabelsRunResult


class _HandlerA(
    code_action.ActionHandler[_LabelsTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> _LabelsRunResult:
        return _LabelsRunResult(labels=["a"])


class _HandlerB(
    code_action.ActionHandler[_LabelsTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> _LabelsRunResult:
        return _LabelsRunResult(labels=["b"])


_ACTION_NAME = _LabelsTestAction.__name__
_ACTION_SOURCE = f"{_LabelsTestAction.__module__}.{_LabelsTestAction.__qualname__}"
_HANDLER_A_SOURCE = f"{_HandlerA.__module__}.{_HandlerA.__qualname__}"
_HANDLER_B_SOURCE = f"{_HandlerB.__module__}.{_HandlerB.__qualname__}"


def _actions(handlers: list[dict]) -> dict[str, dict]:
    return {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": handlers,
        }
    }


async def _run(session) -> schemas.RunActionResponse:
    wal_run_id = str(uuid.uuid4())
    request = schemas.RunActionRequest(action_name=_ACTION_NAME, params={})
    options = schemas.RunActionOptions(
        run_id=wal_run_id,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
            wal_run_id=wal_run_id,
        ),
    )
    return await run_action_service.run_action_raw(
        request=request,
        options=options,
        runner_context=session._runner_context,
    )


def _labels(response: schemas.RunActionResponse) -> list[str]:
    result = response.result_by_format["json"]
    assert isinstance(result, dict)
    return result["labels"]


async def test_only_handlers_bound_to_the_current_env_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matrixed action keeps every variant's handler in the declaration, so
    an ER must execute only the handlers bound to its own env. Running the
    others multiplies every variant's result by the size of the matrix.
    """
    actions = _actions(
        [
            {"name": "a", "source": _HANDLER_A_SOURCE, "env": "env_a"},
            {"name": "b", "source": _HANDLER_B_SOURCE, "env": "env_b"},
        ]
    )
    monkeypatch.setattr(global_state, "env_name", "env_b")
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        response = await _run(session)

    assert _labels(response) == ["b"]


async def test_same_name_handlers_in_other_env_do_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix expansion deep-copies a handler and rewrites only its env, so a
    matrixed action's four entries all share one handler name. The env filter
    must still select exactly one of them.
    """
    actions = _actions(
        [
            {"name": "h", "source": _HANDLER_A_SOURCE, "env": "env_a"},
            {"name": "h", "source": _HANDLER_B_SOURCE, "env": "env_b"},
        ]
    )
    monkeypatch.setattr(global_state, "env_name", "env_b")
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        response = await _run(session)

    assert _labels(response) == ["b"]


async def test_no_env_name_disables_the_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ER launched outside the env-aware CLI (embedded use, test sessions)
    has an empty env name and no env to filter by. It must keep running every
    declared handler rather than refusing.
    """
    actions = _actions(
        [
            {"name": "a", "source": _HANDLER_A_SOURCE, "env": "env_a"},
            {"name": "b", "source": _HANDLER_B_SOURCE, "env": "env_b"},
        ]
    )
    monkeypatch.setattr(global_state, "env_name", "")
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        response = await _run(session)

    assert _labels(response) == ["a", "b"]


async def test_handler_without_env_always_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler with no env is not bound to any interpreter variant and must
    keep running in every ER, alongside whatever env-bound handlers match.
    """
    actions = _actions(
        [
            {"name": "a", "source": _HANDLER_A_SOURCE},
            {"name": "b", "source": _HANDLER_B_SOURCE, "env": "env_b"},
        ]
    )
    monkeypatch.setattr(global_state, "env_name", "env_b")
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        response = await _run(session)

    assert _labels(response) == ["a", "b"]


async def test_no_matching_handler_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a run is addressed to an env with no bound handler, failing loudly is
    the only safe outcome: returning an empty result would silently drop work.
    """
    actions = _actions(
        [{"name": "a", "source": _HANDLER_A_SOURCE, "env": "env_a"}],
    )
    monkeypatch.setattr(global_state, "env_name", "env_b")
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionFailedException) as exc_info:
            await _run(session)

    assert "env_b" in exc_info.value.message
