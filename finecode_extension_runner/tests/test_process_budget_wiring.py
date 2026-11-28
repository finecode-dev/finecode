"""Per-unit budget wiring: runs hold nothing, units lease everything.

A dispatch-only run must reach its handlers without touching the process
budget, while each unit of work it causes leases exactly one slot naming the
run it belongs to.
"""

from __future__ import annotations

import pathlib
import typing

from finecode_extension_runner import (
    context,
    domain,
    er_server,
    run_context,
    services,
)
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.process_slots import ProcessSlots


class _RecordingServer:
    def __init__(self, runner_context: context.RunnerContext) -> None:
        self._runner_context = runner_context
        self._wal_writer = None
        self.requests: list[tuple[str, dict]] = []
        self._leases = 0

    async def send_request_to_wm(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "finecode/leaseProcessBudget":
            self._leases += 1
            return {"leaseId": f"lease-{self._leases}"}
        return {}


def _server(tmp_path: pathlib.Path) -> _RecordingServer:
    project = domain.Project(
        name="test_project",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        actions={},
        action_handler_configs={},
    )
    runner_context = context.RunnerContext(project=project, di_registry=Registry())
    return _RecordingServer(runner_context)


def _run_params() -> dict:
    return {
        "actionName": "some_action",
        "params": {},
        "options": {
            "runId": "test-run-id",
            "meta": {"trigger": "system", "devEnv": "ci"},
        },
    }


def _handlers_params() -> dict:
    return {
        "actionName": "some_action",
        "handlerNames": [],
        "params": {},
        "options": {
            "runId": "test-run-id",
            "meta": {"trigger": "system", "devEnv": "ci"},
        },
    }


async def _fail_run(*args: typing.Any, **kwargs: typing.Any) -> typing.NoReturn:
    raise services.ActionFailedException("stop here")


async def test_runs_hold_no_budget_lease(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    """A run that spawns nothing must not show up in the budget at all."""
    monkeypatch.setattr(er_server.services, "run_action_raw", _fail_run)
    server = _server(tmp_path)
    assert await er_server.run_action(server, _run_params()) == {"error": "stop here"}
    assert [m for m, _ in server.requests if m == "finecode/leaseProcessBudget"] == []


async def test_handler_runs_hold_no_budget_lease(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    """The handler-batch entry point is a dispatch like any other run."""
    monkeypatch.setattr(er_server.services, "run_handlers_raw", _fail_run)
    server = _server(tmp_path)
    assert await er_server.run_handlers(server, _handlers_params()) == {
        "error": "stop here"
    }
    assert [m for m, _ in server.requests if m == "finecode/leaseProcessBudget"] == []


async def test_units_lease_one_slot_naming_their_run(tmp_path: pathlib.Path) -> None:
    """Each unit leases one non-nested slot carrying its run id to the WM."""
    server = _server(tmp_path)
    slots = ProcessSlots(target=8)
    er_server.attach_wm_budget(server, slots)

    with run_context.run("r1"):
        first = await slots.acquire()
        second = await slots.acquire()
        await slots.release(first)
        await slots.release(second)

    leases = [p for m, p in server.requests if m == "finecode/leaseProcessBudget"]
    assert leases == [
        {"requested": 1, "nested": False, "runId": "r1"},
        {"requested": 1, "nested": False, "runId": "r1"},
    ]
    releases = [p for m, p in server.requests if m == "finecode/releaseProcessBudget"]
    assert releases == [{"leaseId": first}, {"leaseId": second}]

    lonely = await slots.acquire()
    await slots.release(lonely)
    leases = [p for m, p in server.requests if m == "finecode/leaseProcessBudget"]
    assert leases[-1] == {"requested": 1, "nested": False, "runId": None}


def test_per_run_lease_and_push_handler_are_gone() -> None:
    """Nothing left answers a gate-target push or leases for a whole run."""
    assert not hasattr(er_server, "update_process_budget")
    assert not hasattr(er_server, "_lease_process_budget")
    assert not hasattr(er_server, "_release_process_budget")
