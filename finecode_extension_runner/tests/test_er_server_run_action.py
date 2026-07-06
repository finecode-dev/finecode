from __future__ import annotations

import pathlib

import finecode_jsonrpc
import pytest

from finecode_extension_runner import context, domain, er_server, services
from finecode_extension_runner.di.registry import Registry


def _fake_server(tmp_path: pathlib.Path) -> object:
    """Minimal stand-in for ErServer exposing only what run_action reads:
    ``_runner_context`` (for project path) and ``_wal_writer`` (None is a
    safe no-op for er_wal.emit_run_event).
    """
    project = domain.Project(
        name="test_project",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        actions={},
        action_handler_configs={},
    )
    runner_context = context.RunnerContext(project=project, di_registry=Registry())

    class _FakeServer:
        _runner_context = runner_context
        _wal_writer = None

    return _FakeServer()


def _run_action_params() -> dict:
    return {
        "actionName": "some_action",
        "params": {},
        "options": {
            "walRunId": "test-run-id",
            "meta": {"trigger": "system", "devEnv": "ci"},
        },
    }


async def test_action_cancelled_exception_is_raised_as_jsonrpc_handler_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled action run must surface at the wire boundary as a genuine
    JSON-RPC RequestCancelled (-32800) error — raised via JsonRpcHandlerError
    so the enclosing JsonRpcServerSession sends a real cancellation response
    to the WM, instead of a generic {"error": ...} payload that would be
    logged and surfaced to the IDE user as a failure.
    """
    async def _raise_cancelled(*args, **kwargs):
        raise services.ActionCancelledException("cancelled by pyrefly")

    monkeypatch.setattr(er_server.services, "run_action_raw", _raise_cancelled)

    server = _fake_server(tmp_path)

    with pytest.raises(finecode_jsonrpc.JsonRpcHandlerError) as exc_info:
        await er_server.run_action(server, _run_action_params())

    assert exc_info.value.code == finecode_jsonrpc.REQUEST_CANCELLED
    assert exc_info.value.message == "cancelled by pyrefly"


async def test_action_failed_exception_still_returns_error_dict(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: an ordinary action failure (not a cancellation) must
    keep returning a plain {"error": ...} response rather than raising —
    the cancellation-specific handling added for ActionCancelledException
    must not change behavior for real failures.
    """
    async def _raise_failed(*args, **kwargs):
        raise services.ActionFailedException("boom")

    monkeypatch.setattr(er_server.services, "run_action_raw", _raise_failed)

    server = _fake_server(tmp_path)

    result = await er_server.run_action(server, _run_action_params())

    assert result == {"error": "boom"}
