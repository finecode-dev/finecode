from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest

from finecode_extension_api import code_action
from finecode_extension_runner import schemas, services
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _RequiredFieldPayload(code_action.RunActionPayload):
    action_source: str


class _RequiredFieldTestAction(
    code_action.Action[
        _RequiredFieldPayload, code_action.RunActionContext, code_action.RunActionResult
    ]
):
    PAYLOAD_TYPE = _RequiredFieldPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = code_action.RunActionResult


class _NoopHandler(
    code_action.ActionHandler[_RequiredFieldTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: _RequiredFieldPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        return code_action.RunActionResult()


_ACTION_NAME = _RequiredFieldTestAction.__name__
_ACTION_SOURCE = f"{_RequiredFieldTestAction.__module__}.{_RequiredFieldTestAction.__qualname__}"
_HANDLER_SOURCE = f"{_NoopHandler.__module__}.{_NoopHandler.__qualname__}"

_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [{"name": "noop", "source": _HANDLER_SOURCE}],
    }
}


async def test_missing_required_payload_field_raises_readable_action_failed_exception(
    tmp_path: Path,
) -> None:
    """A payload missing a required field must fail with a per-field message
    (e.g. "required field missing @ $.action_source"), not the opaque
    "<class 'cattrs.errors.ClassValidationError'>: While structuring ...
    (1 sub-exception)" that a bare ClassValidationError reprs as.

    Exercises ``run_action_raw`` directly (the ``actions/run`` path) rather
    than ``Session.run_action`` (which takes an already-typed payload and so
    never invokes the cattrs structuring this test targets) or
    ``Session.run_handlers`` (which skips structuring entirely for falsy/empty
    params).
    """
    async with handler_test_session(project_dir=tmp_path, actions=_ACTIONS) as session:
        wal_run_id = str(uuid.uuid4())
        request = schemas.RunActionRequest(action_name=_ACTION_NAME, params={})
        options = schemas.RunActionOptions(
            wal_run_id=wal_run_id,
            meta=code_action.RunActionMeta(
                trigger=code_action.RunActionTrigger.SYSTEM,
                dev_env=code_action.DevEnv.CI,
                wal_run_id=wal_run_id,
            ),
        )

        with pytest.raises(services.ActionFailedException) as exc_info:
            await run_action_service.run_action_raw(
                request=request,
                options=options,
                runner_context=session._runner_context,
            )

    message = exc_info.value.message
    assert "cattrs.errors" not in message
    assert "sub-exception" not in message
    assert f"Invalid payload for action {_ACTION_NAME}" in message
    assert "required field missing @ $.action_source" in message
