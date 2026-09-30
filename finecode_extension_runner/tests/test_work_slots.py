"""Explicit work-slot scopes refuse anything that can wait on another run.

A scope holder that waits on a slot it holds deadlocks the machine budget, so
every waiting operation must fail loudly inside one rather than queue silently.
Long-lived processes take no slot at all, so starting one inside a scope stays
legal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys

import finecode_jsonrpc as finecode_jsonrpc_module
import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.iworkslots import WorkSlotScopeError

from finecode_extension_runner import domain, er_errors, er_server
from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.impls.process_executor import ProcessExecutor
from finecode_extension_runner.impls.project_action_runner import (
    ProjectActionRunnerImpl,
)
from finecode_extension_runner.impls.workspace_action_runner import (
    WorkspaceActionRunnerImpl,
)
from finecode_extension_runner.process_slots import ProcessSlots
from finecode_extension_runner.work_slots import WorkSlots


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


def _identity(value: int) -> int:
    return value


@dataclasses.dataclass
class _Payload(code_action.RunActionPayload):
    items: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Result(code_action.RunActionResult):
    values: dict[str, list[str]] = dataclasses.field(default_factory=dict)


class _Action(code_action.Action):
    RESULT_TYPE = _Result


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )


def _ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef.from_type(_Action)


def _slots(target: int = 8) -> tuple[ProcessSlots, WorkSlots]:
    gate = ProcessSlots(target=target)
    return gate, WorkSlots(gate)


def _runner(gate: ProcessSlots) -> CommandRunner:
    return CommandRunner(
        logger=_NoopLogger(), config=CommandRunnerConfig(), process_slots=gate
    )


class _FakeServer:
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.calls: list[str] = []

    async def send_request_to_wm(self, method: str, _params: dict) -> dict:
        self.calls.append(method)
        if self.behavior == "cancelled":
            raise finecode_jsonrpc_module.JsonRpcError(
                {"code": finecode_jsonrpc_module.REQUEST_CANCELLED, "message": "gone"}
            )
        if self.behavior == "error":
            raise finecode_jsonrpc_module.JsonRpcError(
                {"code": -32603, "message": "boom"}
            )
        if method == "finecode/runActionInWorkspace":
            entry = dataclasses.asdict(_Result(values={}))
            return {
                "resultsByProject": {
                    "p1": {"test": {"status": "success", "result": entry}}
                }
            }
        return {}


def _project_runner() -> ProjectActionRunnerImpl:
    source = f"{_Action.__module__}.{_Action.__qualname__}"
    actions = {
        source: domain.ActionDeclaration(
            name="Action",
            config={},
            handlers=[
                domain.ActionHandlerDeclaration(
                    name="h", source=source, config={}, env="test-env"
                )
            ],
            source=source,
        )
    }

    async def _run_action_func(*_args: object, **_kwargs: object) -> _Result:
        return _Result(values={})

    async def _send(_method: str, _params: dict) -> dict:
        raise AssertionError("in-process route must not reach the WM")

    return ProjectActionRunnerImpl(
        send_request_to_wm=_send,  # type: ignore[arg-type]
        run_action_func=_run_action_func,  # type: ignore[arg-type]
        actions_getter=lambda: actions,
        current_env_name_getter=lambda: "test-env",
    )


async def test_command_runner_run_refused_inside_scope() -> None:
    """A bounded spawn inside a scope would wait on the slot the scope holds."""
    gate, scopes = _slots()
    runner = _runner(gate)
    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError, match="starting a bounded process"):
            await runner.run([sys.executable, "-c", "pass"])


async def test_process_executor_submit_refused_inside_scope() -> None:
    """An executor task inside a scope waits on the same gate the scope holds."""
    gate, scopes = _slots()
    executor = ProcessExecutor(process_slots=gate)
    with executor.activate():
        async with scopes.acquire():
            with pytest.raises(WorkSlotScopeError, match="starting a bounded process"):
                await executor.submit(_identity, 1)


async def test_nested_acquire_refused_inside_scope() -> None:
    """Two scopes cannot nest: the inner one would wait on the outer's slot."""
    _, scopes = _slots()
    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError, match="acquiring a work slot"):
            async with scopes.acquire():
                pass  # pragma: no cover


async def test_project_run_action_refused_inside_scope() -> None:
    """Dispatching inside a scope can wait on a run holding the same slot."""
    _, scopes = _slots()
    runner = _project_runner()
    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError, match="dispatching action"):
            await runner.run_action(_ref(), _Payload(), _meta())


async def test_project_run_action_iter_refused_on_first_iteration() -> None:
    """The streaming dispatch is lazy, so the refusal lands on iteration."""
    _, scopes = _slots()
    runner = _project_runner()
    async with scopes.acquire():
        gen = runner.run_action_iter(_ref(), _Payload(), _meta())
        with pytest.raises(WorkSlotScopeError, match="dispatching action"):
            await gen.__anext__()
        await gen.aclose()


async def test_wm_sender_refused_inside_scope_directly() -> None:
    """Any WM request inside a scope can wait on a run holding the slot."""
    _, scopes = _slots()
    sender = er_server.make_wm_request_sender(_FakeServer())  # type: ignore[arg-type]
    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError, match="WM request foo/bar"):
            await sender("foo/bar", {})


async def test_wm_sender_refused_through_workspace_runner() -> None:
    """The workspace fan-out reaches the WM through the same guarded sender."""
    _, scopes = _slots()
    sender = er_server.make_wm_request_sender(_FakeServer())  # type: ignore[arg-type]
    runner = WorkspaceActionRunnerImpl(sender)
    async with scopes.acquire():
        with pytest.raises(WorkSlotScopeError, match="WM request"):
            await runner.run_action_in_projects(_Action, _Payload(), _meta())


async def test_spawned_task_inherits_the_scope() -> None:
    """Context copying marks tasks made inside the scope for their whole life."""
    gate, scopes = _slots()
    runner = _runner(gate)
    async with scopes.acquire():
        task = asyncio.create_task(runner.run([sys.executable, "-c", "pass"]))
        with pytest.raises(WorkSlotScopeError, match="starting a bounded process"):
            await task


async def test_start_long_running_allowed_inside_scope() -> None:
    """A long-lived process takes no slot, so starting one cannot deadlock."""
    gate, scopes = _slots()
    runner = _runner(gate)
    async with scopes.acquire():
        proc = await runner.start_long_running(
            [sys.executable, "-c", "import time; time.sleep(0.1)"]
        )
        await proc.wait_for_end()
    assert gate.in_flight == 0


async def test_same_calls_succeed_outside_scope() -> None:
    """Outside a scope the same operations run normally — the guard is scoped."""
    gate, scopes = _slots()
    runner = _runner(gate)
    executor = ProcessExecutor(process_slots=gate)
    sender = er_server.make_wm_request_sender(_FakeServer())  # type: ignore[arg-type]
    project_runner = _project_runner()

    async with scopes.acquire():
        pass
    proc = await runner.run([sys.executable, "-c", "pass"])
    await proc.wait_for_end()
    with executor.activate():
        assert await executor.submit(_identity, 41) == 41
    assert await sender("foo/bar", {}) == {}
    result = await project_runner.run_action(_ref(), _Payload(), _meta())
    assert isinstance(result, _Result)


async def test_sender_error_translation_unchanged() -> None:
    """Guarding the sender must not change how WM failures are reported."""
    cancelled = er_server.make_wm_request_sender(  # type: ignore[arg-type]
        _FakeServer("cancelled")
    )
    with pytest.raises(er_errors.WmCommunicationCancelled):
        await cancelled("foo/bar", {})
    failed = er_server.make_wm_request_sender(_FakeServer("error"))  # type: ignore[arg-type]
    with pytest.raises(er_errors.WmCommunicationError):
        await failed("foo/bar", {})


async def test_start_long_running_holds_no_slot() -> None:
    """A server process living for seconds must not occupy the work budget."""
    gate = ProcessSlots(target=8)
    runner = _runner(gate)
    proc = await runner.start_long_running(
        [sys.executable, "-c", "import time; time.sleep(0.3)"]
    )
    assert gate.in_flight == 0
    await proc.wait_for_end()
    assert gate.in_flight == 0


async def test_run_holds_slot_until_exit() -> None:
    """A bounded job keeps its slot for its lifetime, not just the spawn."""
    gate = ProcessSlots(target=8)
    runner = _runner(gate)
    proc = await runner.run([sys.executable, "-c", "import time; time.sleep(0.2)"])
    assert gate.in_flight == 1
    await proc.wait_for_end()
    for _ in range(100):
        if gate.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.in_flight == 0


async def test_start_long_running_ignores_local_cap() -> None:
    """The optional local ceiling bounds jobs, not the servers they talk to."""
    gate = ProcessSlots(target=8)
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(max_concurrent_processes=1),
        process_slots=gate,
    )
    long_proc = await runner.start_long_running(
        [sys.executable, "-c", "import time; time.sleep(0.3)"]
    )
    short_proc = await asyncio.wait_for(
        runner.run([sys.executable, "-c", "pass"]), timeout=5
    )
    await short_proc.wait_for_end()
    await long_proc.wait_for_end()
    for _ in range(100):
        if gate.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.in_flight == 0
