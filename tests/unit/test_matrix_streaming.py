from __future__ import annotations

import contextlib
import pathlib
import types
import typing

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.config.interpreter_matrix import (
    Interpreter,
    concrete_env_name,
    parse_interpreter,
)
from finecode.wm_server.runner.runner_client import (
    DevEnv,
    RunActionResponse,
    RunActionTrigger,
    RunResultFormat,
)
from finecode.wm_server.services.run_service import matrix_streaming
from finecode.wm_server.utils.stream_completion import CompletionStream, PrefixFence


def _make_matrix_action(*, interpreters: list[str]) -> domain.Action:
    handlers = [
        domain.ActionHandler(
            name=f"handler_{interpreter}",
            source="test.handlers.TestHandler",
            config={},
            env=concrete_env_name("testing", parse_interpreter(interpreter)),
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

    def __init__(
        self,
        partials: CompletionStream[dict],
        responses: list[RunActionResponse],
    ) -> None:
        self.partials = partials
        self.responses = responses


async def _ack_prefix(fence: PrefixFence) -> None:
    if not fence.completion.done():
        fence.completion.set_result(None)


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
        stream: CompletionStream[dict] = CompletionStream()
        for item in partials:
            stream.append(item)
        stream.end()
        yield _FakeCtx(stream, list(responses))

    return fake_run_with_partial_results


async def _fake_merge_slot_results(
    *,
    project_path: pathlib.Path,
    action_name: str,
    entries: list[dict[str, typing.Any]],
    requested_formats: list[RunResultFormat],
    ws_context: typing.Any,
) -> dict[str, typing.Any]:
    _ = project_path, ws_context
    merged: dict = {}
    for entry in entries:
        payload = entry.get("json")
        if payload:
            merged.update(payload)
    result: dict[str, typing.Any] = {}
    if RunResultFormat.JSON in requested_formats:
        result["json"] = merged
    if RunResultFormat.STRING in requested_formats:
        result["string"] = f"merged {action_name}"
    return result


async def test_two_interpreters_tag_partials_and_combine_by_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

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
    monkeypatch.setattr(
        matrix_streaming, "merge_slot_results", _fake_merge_slot_results
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
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=True,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
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
    assert "string" not in combined_rbf
    assert return_code == 1  # OR of 0 and 1


async def test_variant_failure_is_isolated_as_error_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

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
        result_formats=[RunResultFormat.JSON, RunResultFormat.STRING],
        partial_result_token="tok",
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=False,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    # The healthy variant's partial still made it through.
    assert (
        cpython_311.canonical,
        {"json": {"ok": True}, "string": "all good"},
    ) in received
    # The failing variant is present as an error entry, not silently dropped.
    assert "error" in combined_rbf["json"][cpython_312.canonical]
    assert combined_rbf["json"][cpython_311.canonical] == {"ok": True}
    assert return_code != 0
    assert (
        cpython_312.canonical,
        {"string": "error: boom on cpython@3.12"},
    ) in received


async def test_variant_failure_is_forwarded_as_a_string_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variant that fails must reach a string-format caller's output under
    its own header. Otherwise a merged run only carries the failure inside the
    merged result, which the terminal never prints, and the user sees a bare
    exit code.
    """
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

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

    await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat.STRING],
        partial_result_token="tok",
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=False,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert (
        cpython_312.canonical,
        {"string": "error: boom on cpython@3.12"},
    ) in received


async def test_text_only_merge_keeps_failed_variant_json_after_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )
    scripted = {
        cpython_311.canonical: (
            [
                {"json": {"part": 1}, "string": "first"},
                {"json": {"part": 2}, "string": "second"},
            ],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted, raising={cpython_312.canonical}),
    )
    monkeypatch.setattr(
        matrix_streaming, "merge_slot_results", _fake_merge_slot_results
    )

    received: list[tuple[str, dict]] = []

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    (
        result_by_format,
        return_code,
    ) = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat.STRING],
        partial_result_token="tok",
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=True,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert all(set(value) == {"string"} for _, value in received)
    assert "string" in result_by_format
    assert "error: boom on cpython@3.12" in result_by_format["string"]
    assert result_by_format["json"] == {
        cpython_311.canonical: None,
        cpython_312.canonical: {"error": "boom on cpython@3.12"},
    }
    assert return_code == 1


async def test_text_only_merge_strips_combined_json_when_all_variants_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )
    scripted = {
        cpython_311.canonical: (
            [{"json": {"part": 1}, "string": "first"}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        cpython_312.canonical: (
            [{"json": {"part": 2}, "string": "second"}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted),
    )
    monkeypatch.setattr(
        matrix_streaming, "merge_slot_results", _fake_merge_slot_results
    )

    received: list[tuple[str, dict]] = []

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    (
        result_by_format,
        return_code,
    ) = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat.STRING],
        partial_result_token="tok",
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=True,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert all(set(value) == {"string"} for _, value in received)
    assert "string" in result_by_format
    assert "json" not in result_by_format
    assert return_code == 0


async def test_direct_result_is_filtered_and_merged_as_one_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = Interpreter("cpython", "3.11")
    action = _make_matrix_action(interpreters=[interpreter.canonical])
    scripted = {
        interpreter.canonical: (
            [],
            [
                RunActionResponse(
                    result_by_format={"json": {"direct": True}, "string": "direct"},
                    return_code=0,
                    status="success",
                )
            ],
        ),
    }
    monkeypatch.setattr(
        matrix_streaming.proxy_utils,
        "run_with_partial_results",
        _make_fake_run_with_partial_results(scripted),
    )
    received: list[tuple[str, dict]] = []
    captured_entries: list[list[dict[str, typing.Any]]] = []

    async def _merge_slot_results(
        *, entries: list[dict[str, typing.Any]], **_: typing.Any
    ) -> dict[str, typing.Any]:
        captured_entries.append(entries)
        return {"string": "merged direct"}

    async def on_partial(interpreter_canonical: str, result_by_format: dict) -> None:
        received.append((interpreter_canonical, result_by_format))

    monkeypatch.setattr(matrix_streaming, "merge_slot_results", _merge_slot_results)

    (
        result_by_format,
        return_code,
    ) = await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat.STRING],
        partial_result_token="tok",
        run_trigger=RunActionTrigger.USER,
        dev_env=DevEnv.CLI,
        ws_context=context.WorkspaceContext(ws_dirs_paths=[]),
        merge_results=True,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert received == [(interpreter.canonical, {"string": "direct"})]
    assert captured_entries == [[{"json": {"direct": True}, "string": "direct"}]]
    assert result_by_format == {"string": "=== cpython@3.11 ===\nmerged direct"}
    assert return_code == 0


async def test_variant_failure_is_not_forwarded_to_a_json_only_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that asked only for JSON must not receive the string-format
    error partial. This separates "filtered by requested format" from "the
    check never matches", which the result_formats=None case alone cannot.
    """
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

    scripted = {
        cpython_311.canonical: (
            [{"json": {"ok": True}}],
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

    await matrix_streaming.run_matrix_with_partial_results(
        project=_make_project(),
        action=action,
        action_name="test_action",
        params={},
        result_formats=[RunResultFormat.JSON],
        partial_result_token="tok",
        run_trigger=None,
        dev_env=None,
        ws_context=None,
        merge_results=False,
        on_partial=on_partial,
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert received == [(cpython_311.canonical, {"json": {"ok": True}})]


async def test_merge_results_false_still_keys_by_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

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
        on_prefix=_ack_prefix,
        origin=None,
    )

    assert combined_rbf["json"] == {
        cpython_311.canonical: {"ok": True},
        cpython_312.canonical: {"ok": False},
    }
    assert return_code == 0


async def test_selected_envs_restricts_fan_out_to_that_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
    )

    scripted = {
        cpython_311.canonical: (
            [{"json": {"ok": True}}],
            [RunActionResponse(result_by_format={}, return_code=0, status="streamed")],
        ),
        # cpython_312 not scripted — its variant must never be looked up when
        # it is excluded from `selected_envs`.
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
        on_prefix=_ack_prefix,
        selected_envs={concrete_env_name("testing", cpython_311)},
        origin=None,
    )

    assert {tag for tag, _ in received} == {cpython_311.canonical}
    assert set(combined_rbf["json"].keys()) == {cpython_311.canonical}
    assert return_code == 0


async def test_selection_naming_no_env_runs_nothing_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpython_311 = Interpreter("cpython", "3.11")
    cpython_312 = Interpreter("cpython", "3.12")
    action = _make_matrix_action(
        interpreters=[cpython_311.canonical, cpython_312.canonical]
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
        on_prefix=_ack_prefix,
        selected_envs={"other-cpython-3.14"},
        origin=None,
    )

    assert received == []
    assert combined_rbf == {}
    assert return_code == 0
