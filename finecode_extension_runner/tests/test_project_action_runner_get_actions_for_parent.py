from __future__ import annotations

import dataclasses

import pytest

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner import domain, er_errors
from finecode_extension_runner.impls import project_action_runner


@dataclasses.dataclass
class _ParentPayload(code_action.RunActionPayload):
    marker: str = ""


@dataclasses.dataclass
class _ParentResult(code_action.RunActionResult):
    messages: dict = dataclasses.field(default_factory=dict)

    def update(self, other: _ParentResult) -> None:
        self.messages.update(other.messages)


class _ParentAction(code_action.Action[_ParentPayload, code_action.RunActionContext, _ParentResult]):
    PAYLOAD_TYPE = _ParentPayload
    RESULT_TYPE = _ParentResult


@dataclasses.dataclass
class _PythonPayload(_ParentPayload):
    # Extra field with a default, per ADR-0008's contract for subaction payload
    # extensions — dispatch-based invocation must be constructible without it.
    strict: bool = False


class _PythonSubAction(
    code_action.Action[_PythonPayload, code_action.RunActionContext, _ParentResult]
):
    PAYLOAD_TYPE = _PythonPayload
    RESULT_TYPE = _ParentResult
    LANGUAGE = "python"
    PARENT_ACTION = _ParentAction


def _source_of(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _make_runner(
    *,
    actions: dict[str, domain.ActionDeclaration] | None = None,
    send_request_to_wm=None,
    env_name: str = "dev_workspace",
) -> project_action_runner.ProjectActionRunnerImpl:
    async def _default_send(method: str, params: dict):
        raise AssertionError(f"unexpected WM call: {method}({params})")

    async def _run_action_func(*args, **kwargs):
        raise AssertionError("run_action_func should not be called in these tests")

    return project_action_runner.ProjectActionRunnerImpl(
        send_request_to_wm=send_request_to_wm or _default_send,
        run_action_func=_run_action_func,
        actions_getter=lambda: actions or {},
        current_env_name_getter=lambda: env_name,
    )


async def test_get_actions_for_parent_resolves_locally_importable_subaction() -> None:
    """A subaction whose handler runs in this ER's own env is resolved via the
    local fast path — no WM data is needed to find it, only to confirm there
    are no other cross-env subactions."""
    action_def = domain.ActionDeclaration(
        name="check_python_imports",
        config={},
        handlers=[],
        source=_source_of(_PythonSubAction),
    )

    async def _send(method: str, params: dict):
        assert method == "finecode/getActionsForParent"
        return {"subactions": []}

    runner = _make_runner(actions={"check_python_imports": action_def}, send_request_to_wm=_send)

    result = await runner.get_actions_for_parent(_ParentAction)

    assert set(result.keys()) == {"python"}
    assert result["python"].action_type is _PythonSubAction
    assert result["python"].source == _source_of(_PythonSubAction)


async def test_get_actions_for_parent_discovers_cross_env_subaction_via_wm() -> None:
    """A subaction whose handler lives in another env is entirely absent from
    this ER's own action list (each ER only ever receives the actions its own
    env executes), so it can only be discovered by asking the WM — local
    enumeration alone would report no subaction for that language at all.

    The WM's answer reports a config *alias* distinct from the canonical
    source (as it legitimately can — see ADR-0019/ADR-0021): the merged
    ``ActionRef.source`` must be the canonical source, not the alias, because
    that's what ``run_action`` sends as ``actionSource`` to
    ``finecode/runActionInProject``, which resolves strictly by canonical
    source and rejects aliases.
    """
    calls: list[dict] = []
    alias = "fine_python_lang.CheckPythonImportsAction"
    canonical = "fine_python_lang.check_python_imports_action.CheckPythonImportsAction"

    async def _send(method: str, params: dict):
        calls.append(params)
        assert method == "finecode/getActionsForParent"
        assert params == {"parentActionSource": _source_of(_ParentAction)}
        return {
            "subactions": [
                {
                    "source": alias,
                    "canonicalSource": canonical,
                    "language": "python",
                }
            ]
        }

    # No local actions at all — mirrors the dev_workspace ER's real view when
    # the subaction's only handler runs in a different env (e.g. dev_no_runtime).
    runner = _make_runner(actions={}, send_request_to_wm=_send)

    result = await runner.get_actions_for_parent(_ParentAction)

    assert len(calls) == 1
    assert set(result.keys()) == {"python"}
    assert result["python"].action_type is None
    assert result["python"].source == canonical
    assert result["python"].result_type is _ParentResult


async def test_get_actions_for_parent_prefers_local_resolution_over_wm_echo() -> None:
    """The WM's answer may legitimately include a subaction already resolved
    locally (it doesn't know what this ER already found) — that must not be
    treated as a language conflict, and the locally-typed ActionRef wins."""
    action_def = domain.ActionDeclaration(
        name="check_python_imports",
        config={},
        handlers=[],
        source=_source_of(_PythonSubAction),
    )

    async def _send(method: str, params: dict):
        return {
            "subactions": [
                {
                    "source": _source_of(_PythonSubAction),
                    "canonicalSource": _source_of(_PythonSubAction),
                    "language": "python",
                }
            ]
        }

    runner = _make_runner(actions={"check_python_imports": action_def}, send_request_to_wm=_send)

    result = await runner.get_actions_for_parent(_ParentAction)

    assert result["python"].action_type is _PythonSubAction


async def test_get_actions_for_parent_raises_instead_of_swallowing_wm_failure() -> None:
    """A WM communication failure must surface as an error, not be silently
    downgraded to an incomplete-but-successful result — otherwise a transient
    WM problem looks identical to "no subaction exists"."""

    async def _send(method: str, params: dict):
        raise er_errors.WmCommunicationError("boom")

    runner = _make_runner(actions={}, send_request_to_wm=_send)

    with pytest.raises(iprojectactionrunner.ActionRunFailed):
        await runner.get_actions_for_parent(_ParentAction)


async def test_get_actions_for_parent_raises_cancelled_on_wm_cancellation() -> None:
    async def _send(method: str, params: dict):
        raise er_errors.WmCommunicationCancelled("cancelled")

    runner = _make_runner(actions={}, send_request_to_wm=_send)

    with pytest.raises(iprojectactionrunner.ActionRunCancelled):
        await runner.get_actions_for_parent(_ParentAction)


async def test_run_action_coerces_payload_to_locally_known_subaction_type() -> None:
    """Dispatch handlers build payloads against the *parent* action's payload
    type generically (they can't import a subaction living in another env to
    build its own PAYLOAD_TYPE). When the subaction *is* locally importable,
    run_action must upgrade the payload to its concrete PAYLOAD_TYPE itself —
    callers must not need to branch on ``action_type is None`` to do this.
    """
    captured: dict = {}

    async def _run_action_func(action_def, payload, meta, **kwargs):
        captured["payload"] = payload
        return _ParentResult(messages={})

    action_def = domain.ActionDeclaration(
        name="check_python_imports",
        config={},
        handlers=[domain.ActionHandlerDeclaration(name="h", source="x.H", config={}, env="dev_workspace")],
        source=_source_of(_PythonSubAction),
    )
    runner = project_action_runner.ProjectActionRunnerImpl(
        send_request_to_wm=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no WM call expected")),
        run_action_func=_run_action_func,
        actions_getter=lambda: {"check_python_imports": action_def},
        current_env_name_getter=lambda: "dev_workspace",
    )

    subaction = iprojectactionrunner.ActionRef(
        source=_source_of(_PythonSubAction),
        result_type=_ParentResult,
        action_type=_PythonSubAction,
    )
    await runner.run_action(
        action_type=subaction,
        payload=_ParentPayload(marker="m"),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CI,
            orchestration_depth=0,
        ),
    )

    assert isinstance(captured["payload"], _PythonPayload)
    assert captured["payload"].marker == "m"
    assert captured["payload"].strict is False


async def test_run_action_leaves_payload_as_is_when_subaction_type_unknown() -> None:
    """When the subaction lives in another env (``action_type`` is ``None``),
    there is no concrete type to coerce to locally — the base payload is sent
    over the WM as-is, and the receiving env structures it into its own
    PAYLOAD_TYPE, defaulting fields absent from the base type (ADR-0008)."""
    captured: dict = {}

    async def _send(method: str, params: dict):
        captured["params"] = params
        return {"result": {"messages": {}}, "returnCode": 0}

    runner = _make_runner(actions={}, send_request_to_wm=_send)

    subaction = iprojectactionrunner.ActionRef(
        source=_source_of(_PythonSubAction),
        result_type=_ParentResult,
        action_type=None,
    )
    await runner.run_action(
        action_type=subaction,
        payload=_ParentPayload(marker="m"),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CI,
            orchestration_depth=0,
        ),
    )

    assert captured["params"]["payload"] == {"marker": "m"}
