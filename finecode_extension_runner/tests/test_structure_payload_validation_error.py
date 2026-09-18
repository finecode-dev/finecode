from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from finecode_extension_api import code_action

from finecode_extension_runner import schemas, services
from finecode_extension_runner._converter import converter as _shared_converter
from finecode_extension_runner._converter import payload_converter as _payload_converter
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
_ACTION_SOURCE = (
    f"{_RequiredFieldTestAction.__module__}.{_RequiredFieldTestAction.__qualname__}"
)
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
            run_id=wal_run_id,
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


@dataclasses.dataclass
class _OptionalFieldPayload(code_action.RunActionPayload):
    opt_i: int | None = None
    opt_s: str | None = None
    items: list[str] = dataclasses.field(default_factory=list)


class _OptionalFieldTestAction(
    code_action.Action[
        _OptionalFieldPayload,
        code_action.RunActionContext,
        code_action.RunActionResult,
    ]
):
    PAYLOAD_TYPE = _OptionalFieldPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = code_action.RunActionResult


class _OptionalNoopHandler(
    code_action.ActionHandler[_OptionalFieldTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: _OptionalFieldPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        return code_action.RunActionResult()


_OPTIONAL_ACTION_NAME = _OptionalFieldTestAction.__name__
_OPTIONAL_ACTION_SOURCE = (
    f"{_OptionalFieldTestAction.__module__}.{_OptionalFieldTestAction.__qualname__}"
)
_OPTIONAL_HANDLER_SOURCE = (
    f"{_OptionalNoopHandler.__module__}.{_OptionalNoopHandler.__qualname__}"
)

_OPTIONAL_ACTIONS = {
    _OPTIONAL_ACTION_NAME: {
        "source": _OPTIONAL_ACTION_SOURCE,
        "handlers": [{"name": "noop", "source": _OPTIONAL_HANDLER_SOURCE}],
    }
}


async def test_optional_field_rejects_a_wrong_type(
    tmp_path: Path,
) -> None:
    """An ``int | None`` field must not accept a string silently.

    The union fallback used to pass an unmatched value through unchanged, so
    ``{"opt_i": "abc"}`` reached a handler with a string where it declared an
    optional int.  That corrupts every caller that trusted the declared type.
    """
    async with handler_test_session(
        project_dir=tmp_path, actions=_OPTIONAL_ACTIONS
    ) as session:
        wal_run_id = str(uuid.uuid4())
        request = schemas.RunActionRequest(
            action_name=_OPTIONAL_ACTION_NAME, params={"opt_i": "abc"}
        )
        options = schemas.RunActionOptions(
            run_id=wal_run_id,
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

    assert "opt_i" in exc_info.value.message


async def test_extra_keys_are_still_tolerated(tmp_path: Path) -> None:
    """A payload carrying fields the action does not declare must still run.

    Cross-env subaction dispatch ships the parent payload's dict to a subaction
    whose concrete payload type is not importable here; the receiving env must
    ignore the parent-only fields, not fail the run.
    """
    async with handler_test_session(
        project_dir=tmp_path, actions=_OPTIONAL_ACTIONS
    ) as session:
        wal_run_id = str(uuid.uuid4())
        request = schemas.RunActionRequest(
            action_name=_OPTIONAL_ACTION_NAME,
            params={"opt_i": 2, "parent_only": "ignored"},
        )
        options = schemas.RunActionOptions(
            run_id=wal_run_id,
            meta=code_action.RunActionMeta(
                trigger=code_action.RunActionTrigger.SYSTEM,
                dev_env=code_action.DevEnv.CI,
                wal_run_id=wal_run_id,
            ),
        )

        response = await run_action_service.run_action_raw(
            request=request,
            options=options,
            runner_context=session._runner_context,
        )

    assert response.return_code == code_action.RunReturnCode.SUCCESS.value


def test_the_shared_converter_is_unchanged_and_distinct() -> None:
    """Strict typing must not leak into results, run state or config structuring."""
    payload = _shared_converter.structure({"bogus": 1}, _OptionalFieldPayload)

    assert payload.opt_i is None
    assert _shared_converter is not _payload_converter


def test_the_shared_converter_still_passes_unmatched_union_values_through() -> None:
    payload = _shared_converter.structure({"opt_i": "abc"}, _OptionalFieldPayload)

    assert payload.opt_i == "abc"
