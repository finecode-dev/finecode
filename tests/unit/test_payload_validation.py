"""Payload names and types are validated at the CLI, guided by each action's schema.

The same payload is sent to every action in a run, so the CLI is the last place
that knows both the literal ``--field=value`` text and the declared field type.
These tests pin down the two silent failures that contract closes: an unknown
field name silently dropped by the ER, and a scalar silently coerced into a
list (or a numeric-looking string into a number) before anything runs.
"""

from __future__ import annotations

import pathlib

import pytest
from finecode_extension_runner.schema_utils import JsonValue, PayloadSchema
from loguru import logger

from finecode.cli_app.commands import run_cmd
from finecode.wm_client import ApiError

_Schemas = dict[str, PayloadSchema | None]


class _FakeClient:
    def __init__(
        self,
        schemas: _Schemas | None = None,
        error: ApiError | None = None,
    ) -> None:
        self._schemas: _Schemas = schemas if schemas is not None else {}
        self._error = error
        self.calls: list[tuple[str, bool]] = []

    async def get_payload_schemas(
        self, project: str, action_sources: list[str], *, start_runners: bool = False
    ) -> _Schemas:
        self.calls.append((project, start_runners))
        if self._error is not None:
            raise self._error
        return self._schemas


async def _resolve(
    schemas: _Schemas | None = None,
    *,
    error: ApiError | None = None,
    raw: dict[str, str] | None = None,
    action_payload: dict[str, JsonValue] | None = None,
    map_payload_fields: set[str] | None = None,
) -> dict[str, JsonValue]:
    return await run_cmd._resolve_payload(
        client=_FakeClient(schemas, error),
        action_payload=action_payload if action_payload is not None else {},
        raw_action_payload=raw if raw is not None else {},
        action_sources=["src.Action"],
        schema_project="/ws",
        base_dir=pathlib.Path("/ws"),
        map_payload_fields=map_payload_fields,
    )


async def test_unknown_field_is_refused_with_suggestion_and_valid_names() -> None:
    """An unknown name must not be silently dropped by the ER."""
    schemas = {
        "src.Action": {
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                },
                "strict": {"type": "boolean"},
            }
        }
    }

    with pytest.raises(run_cmd.RunFailed) as exc_info:
        await _resolve(
            schemas=schemas,
            raw={"file_path": '["file:///ws/a"]'},
            action_payload={"file_path": ["file:///ws/a"]},
        )

    message = exc_info.value.message
    assert "file_path" in message
    assert "did you mean 'file_paths'?" in message
    assert "Valid fields:" in message


async def test_a_far_miss_gets_no_suggestion() -> None:
    schemas = {"src.Action": {"properties": {"file_paths": {"type": "array"}}}}

    with pytest.raises(run_cmd.RunFailed) as exc_info:
        await _resolve(
            schemas=schemas,
            raw={"zzzz": "1"},
            action_payload={"zzzz": 1},
        )

    assert "did you mean" not in exc_info.value.message


async def test_an_unknown_field_runs_when_the_schema_is_missing() -> None:
    """An unschemad action's fields are unknown, so no name is refused for it."""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        result = await _resolve(
            schemas={"src.Action": None},
            raw={"totally_bogus_param": "42"},
            action_payload={"totally_bogus_param": 42},
        )
    finally:
        logger.remove(sink_id)

    assert result == {"totally_bogus_param": 42}
    assert any("Skipping payload field name check" in message for message in messages)


async def test_a_schema_fetch_failure_refuses_the_run() -> None:
    """A schema the CLI cannot read means path values cannot be converted, so
    dispatching anyway would send a path the CLI could not vouch for. The
    refusal names the action and the project so the cause is visible.
    """
    with pytest.raises(run_cmd.RunFailed) as exc_info:
        await _resolve(
            error=ApiError(),
            raw={"totally_bogus_param": "42"},
            action_payload={"totally_bogus_param": 42},
        )

    message = exc_info.value.message
    assert "src.Action" in message
    assert "/ws" in message


async def test_schemas_are_requested_with_start_runners() -> None:
    """The CLI must ask the WM to start the handler environment before probing,
    so a cold WM still returns the schema the path conversion depends on.
    """
    client = _FakeClient(schemas={"src.Action": {"properties": {}}})

    await run_cmd._resolve_payload(
        client=client,
        action_payload={},
        raw_action_payload={},
        action_sources=["src.Action"],
        schema_project="/ws",
        base_dir=pathlib.Path("/ws"),
        map_payload_fields=None,
    )

    assert client.calls == [("/ws", True)]


def test_choose_schema_project_prefers_an_explicit_project_path() -> None:
    assert (
        run_cmd._choose_schema_project(
            ["/ws/target"],
            [{"project": "/ws", "source": "src.Action"}],
            ["src.Action"],
            pathlib.Path("/ws"),
        )
        == "/ws/target"
    )


def test_choose_schema_project_prefers_base_dir_when_it_exposes_the_action() -> None:
    assert (
        run_cmd._choose_schema_project(
            None,
            [
                {"project": "/ws/other", "source": "src.Action"},
                {"project": "/ws", "source": "src.Action"},
            ],
            ["src.Action"],
            pathlib.Path("/ws"),
        )
        == "/ws"
    )


def test_choose_schema_project_falls_back_to_the_first_listed_action() -> None:
    assert (
        run_cmd._choose_schema_project(
            None,
            [
                {"project": "/ws/other", "source": "src.Other"},
                {"project": "/ws/pkg", "source": "src.Action"},
            ],
            ["src.Action"],
            pathlib.Path("/ws"),
        )
        == "/ws/pkg"
    )


async def test_missing_schema_with_payload_logs_a_warning() -> None:
    """A schema that is None while payload fields were sent means those fields
    skip both name validation and type conversion. The run continues, but the
    operator must be able to see why it was lenient.
    """
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        await _resolve(
            schemas={"src.Action": None},
            raw={"totally_bogus_param": "42"},
            action_payload={"totally_bogus_param": 42},
        )
    finally:
        logger.remove(sink_id)

    assert any("Skipping payload field name check" in message for message in messages)


async def test_a_scalar_where_a_list_is_declared_is_refused_at_the_cli() -> None:
    schemas = {
        "src.Action": {
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                }
            }
        }
    }

    with pytest.raises(run_cmd.RunFailed) as exc_info:
        await _resolve(
            schemas=schemas,
            raw={"file_paths": "file:///ws/pkg/__init__.py"},
            action_payload={"file_paths": "file:///ws/pkg/__init__.py"},
        )

    message = exc_info.value.message
    assert "file_paths" in message
    assert "expected a list" in message
    assert "['a', 'b']" in message
    assert "Cannot convert non-file URI to Path" not in message


async def test_a_numeric_looking_string_stays_a_string() -> None:
    schemas = {"src.Action": {"properties": {"version": {"type": "string"}}}}

    result = await _resolve(
        schemas=schemas,
        raw={"version": "1.0"},
        action_payload={"version": 1.0},
    )

    assert result == {"version": "1.0"}


async def test_mapped_fields_skip_coercion_and_type_validation() -> None:
    """A mapped field holds a ``"action.field"`` placeholder, not its real type."""
    schemas = {
        "src.Action": {
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                }
            }
        }
    }

    result = await _resolve(
        schemas=schemas,
        raw={"file_paths": "lint.diagnostics"},
        action_payload={"file_paths": "lint.diagnostics"},
        map_payload_fields={"file_paths"},
    )

    assert result == {"file_paths": "lint.diagnostics"}
