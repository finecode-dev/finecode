from __future__ import annotations

import contextlib
import pathlib
import types
import typing

import pytest

from finecode.wm_server import domain
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner.runner_client import RunActionResponse
from finecode.wm_server.services.run_service import matrix_streaming
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


def _make_project() -> typing.Any:
    """A minimal stand-in for a ``domain.CollectedProject``.

    ``run_matrix_with_partial_results`` only ever reads ``project.dir_path``
    off it (it forwards ``action`` separately), so a lightweight namespace is
    enough — no need to build a full collected project.
    """
    return types.SimpleNamespace(dir_path=pathlib.Path("/fake/project"))


class _FakeCtx:
    """Stands in for ``proxy_utils.RunWithPartialResultsContext``."""

    def __init__(self, partials: list[dict], responses: list[RunActionResponse]) -> None:
        self._partials = partials
        self.responses = responses

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for item in self._partials:
            yield item


def _make_fake_run_with_partial_results(
    scripted: dict[str, tuple[list[dict], list[RunActionResponse]]],
    raising: set[str] | None = None,
):
    """Build a stand-in for ``proxy_utils.run_with_partial_results``.

    *scripted* maps an interpreter's canonical string to the partials it
    streams and the final responses ``ctx.responses`` should carry after the
    context manager exits. Interpreters in *raising* raise instead of
    yielding a context, to exercise variant isolation.
    """
    raising = raising or set()

    @contextlib.asynccontextmanager
    async def fake_run_with_partial_results(*, interpreter=None, **kwargs):
        canonical = interpreter.canonical if interpreter is not None else None
        if canonical in raising:
            raise RuntimeError(f"boom on {canonical}")
        partials, responses = scripted[canonical]
        yield _FakeCtx(list(partials), list(responses))

    return fake_run_with_partial_results


async def _fake_merge(
    *, project_path: pathlib.Path, action_name: str, json_payloads: list[dict], ws_context
) -> dict | None:
    merged: dict = {}
    for payload in json_payloads:
        if payload:
            merged.update(payload)
    return merged or None


async def test_two_interpreters_tag_partials_and_combine_by_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(interpreters=[cpython_311.canonical, cpython_312.canonical])

    scripted = {
        cpython_311.canonical: (
            [
                {"json": {"a": 1}, "string": "first"},
                {"json": {"a": 2}, "string": "second"},
            ],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        cpython_312.canonical: (
            [{"json": {"b": 1}}],
            [RunActionResponse(result_by_format={}, return_code=1, status="streamed")],
        ),
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted),
    )
    monkeypatch.setattr(matrix_streaming, "merge_partial_results_for_action", _fake_merge)

    received: list[tuple[str, dict]] = []

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    combined_rbf, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=None,
        partial_result_token="tok",
        run_trigger=None,
        dev_env=None,
        ws_context=None,
        merge_results=True,
        on_partial=on_partial,
    )

    tagged_interpreters = {tag for tag, _ in received}
    assert tagged_interpreters == {cpython_311.canonical, cpython_312.canonical}
    # Every partial streamed for each variant is forwarded individually.
    assert len([t for t, _ in received if t == cpython_311.canonical]) == 2
    assert len([t for t, _ in received if t == cpython_312.canonical]) == 1

    assert combined_rbf["json"] == {
        cpython_311.canonical: {"a": 2},
        cpython_312.canonical: {"b": 1},
    }
    assert f"=== {cpython_311.canonical} ===" in combined_rbf["string"]
    assert "second" in combined_rbf["string"]
    assert return_code == 1  # OR of 0 and 1


async def test_variant_failure_is_isolated_as_error_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(interpreters=[cpython_311.canonical, cpython_312.canonical])

    scripted = {
        cpython_311.canonical: (
            [{"json": {"ok": True}, "string": "all good"}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        # cpython_312 not scripted — its variant raises before being looked up.
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted, raising={cpython_312.canonical}),
    )

    received: list[tuple[str, dict]] = []

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    combined_rbf, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=None,
        partial_result_token="tok",
        run_trigger=None,
        dev_env=None,
        ws_context=None,
        merge_results=False,
        on_partial=on_partial,
    )

    # The healthy variant's partial still made it through.
    assert (cpython_311.canonical, {"json": {"ok": True}, "string": "all good"}) in received
    # The failing variant is present as an error entry, not silently dropped.
    assert "error" in combined_rbf["json"][cpython_312.canonical]
    assert combined_rbf["json"][cpython_311.canonical] == {"ok": True}
    assert return_code != 0


async def test_merge_results_false_still_keys_by_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(interpreters=[cpython_311.canonical, cpython_312.canonical])

    scripted = {
        cpython_311.canonical: (
            [{"json": {"ok": True}}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        cpython_312.canonical: (
            [{"json": {"ok": False}}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted),
    )

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        pass

    combined_rbf, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=None,
        partial_result_token="tok",
        run_trigger=None,
        dev_env=None,
        ws_context=None,
        merge_results=False,
        on_partial=on_partial,
    )

    assert combined_rbf["json"] == {
        cpython_311.canonical: {"ok": True},
        cpython_312.canonical: {"ok": False},
    }
    assert return_code == 0


async def test_selected_interpreters_restricts_fan_out_to_that_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(interpreters=[cpython_311.canonical, cpython_312.canonical])

    scripted = {
        cpython_311.canonical: (
            [{"json": {"ok": True}}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        # cpython_312 not scripted — its variant must never be looked up when
        # it is excluded from `selected_interpreters`.
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted),
    )

    received: list[tuple[str, dict]] = []

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    combined_rbf, return_code = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=None,
        partial_result_token="tok",
        run_trigger=None,
        dev_env=None,
        ws_context=None,
        merge_results=False,
        on_partial=on_partial,
        selected_interpreters={cpython_311.canonical},
    )

    assert {tag for tag, _ in received} == {cpython_311.canonical}
    assert set(combined_rbf["json"].keys()) == {cpython_311.canonical}
    assert return_code == 0


async def test_unknown_selected_interpreter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(interpreters=[cpython_311.canonical, cpython_312.canonical])

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        pass

    with pytest.raises(ActionRunFailed):
        await matrix_streaming.run_matrix_with_partial_results(
            project=_make_project(),
            action=action,
            action_name="test_action",
            params={},
            result_formats=None,
            partial_result_token="tok",
            run_trigger=None,
            dev_env=None,
            ws_context=None,
            merge_results=False,
            on_partial=on_partial,
            selected_interpreters={"cpython@3.14"},
        )
