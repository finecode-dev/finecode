"""Regression tests for issue #12, against a real WM and a real ER.

The in-process guard (`tests/unit/test_repro_reload_hang.py`) proves the
mechanism with a fake client. This drives the real thing: it freezes the
running ER with ``SIGSTOP`` -- the OS process stays alive and the WM still
reports it ``RUNNING``, but it answers no RPC -- then asks the WM to restart
or reload that project.

A runner in that state used to hang the call forever, because an unbounded
``send_request`` on the WM->ER channel never resolved. These regression tests
assert the call completes within a finite bound and leaves no frozen
interpreter behind: an orphaned ER keeps using a port and its environment,
which the next recovery cannot clean up.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
import sys
import time

import psutil
import pytest

from tests.e2e.wm.test_recovery import wm_with_er  # noqa: F401  (pytest fixture)


def _er_pids(parent_pid: int) -> list[int]:
    """The *interpreter* pids that run an ER under *parent_pid* (the WM).

    The WM launches an ER as ``/bin/sh -c "<venv>/bin/python -m
    finecode_extension_runner.cli start ..."`` -- the shell is a direct child
    and the real ER is its child. Stopping the shell leaves the ER serving RPC,
    so select the ``python`` process, not the ``sh`` wrapper.
    """
    found: list[int] = []
    for child in psutil.Process(parent_pid).children(recursive=True):
        try:
            cmdline = child.cmdline()
        except psutil.Error:
            continue
        if not cmdline or "/bin/sh" in cmdline[0]:
            continue
        if any("finecode_extension_runner" in arg for arg in cmdline):
            found.append(child.pid)
    return found


def _freeze_ers(parent_pid: int) -> list[int]:
    er_pids = _er_pids(parent_pid)
    assert er_pids, (
        f"no finecode_extension_runner interpreter found under WM pid {parent_pid}"
    )
    for pid in er_pids:
        os.kill(pid, signal.SIGSTOP)
    return er_pids


def _thaw(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


def _state(pid: int) -> str | None:
    try:
        return psutil.Process(pid).status()
    except psutil.ZombieProcess:  # subclass of NoSuchProcess: must come first
        return psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return None


def _wait_until_pids_gone(pids: list[int], timeout: float = 5.0) -> None:
    """Wait for every pid to be gone or a zombie, so no orphan survives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_state(pid) in (None, psutil.STATUS_ZOMBIE) for pid in pids):
            return
        time.sleep(0.1)
    raise AssertionError(
        f"ER interpreter(s) survived the recovery: "
        f"{[(pid, _state(pid)) for pid in pids]}"
    )


async def _wait_for_running_runner(
    client, workspace_dir: pathlib.Path, timeout: float = 90.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runners = await client.list_runners()
        if any(
            r["projectPath"] == str(workspace_dir)
            and r["envName"] == "dev_workspace"
            and r["status"] == "RUNNING"
            for r in runners
        ):
            return
        await asyncio.sleep(0.25)
    raise AssertionError("dev_workspace runner never reached RUNNING")


async def _freeze_running_er(client, workspace_dir: pathlib.Path) -> list[int]:
    """Wait for the ER, assert it is RUNNING, then freeze it.

    While frozen, ``list_runners`` must still answer -- the WM's loop is fine;
    only the ER-channel-dependent paths are stuck.
    """
    await _wait_for_running_runner(client, workspace_dir)
    info = await client.get_info()
    frozen = _freeze_ers(info["pid"])
    await asyncio.sleep(0.25)
    assert all(_state(pid) == psutil.STATUS_STOPPED for pid in frozen), (
        f"ER interpreter(s) {frozen} did not enter the stopped state; "
        "the freeze would not actually block RPC"
    )
    assert [r["status"] for r in await client.list_runners()] == ["RUNNING"], (
        "list_runners stopped answering after the ER was frozen -- the WM loop, "
        "not just the ER channel, is affected"
    )
    return frozen


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGSTOP")
async def test_restart_runner_completes_when_the_er_is_frozen(wm_with_er) -> None:
    """Restarting a project whose ER stopped answering must complete and reap it."""
    client, workspace_dir = wm_with_er
    frozen = await _freeze_running_er(client, workspace_dir)
    try:
        await asyncio.wait_for(
            client.restart_runner(project=str(workspace_dir), env="dev_workspace"),
            timeout=45.0,
        )
        _wait_until_pids_gone(frozen)
    finally:
        _thaw(frozen)


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGSTOP")
async def test_reload_config_completes_when_the_er_is_frozen(wm_with_er) -> None:
    """The reported operation: reload_config against a frozen ER must complete.

    The recovery may fail rather than replace the runner — the config re-read
    asks the frozen ER first — but it must return with a visible status, and the
    frozen interpreter must be reaped rather than left behind as an orphan.
    """
    client, workspace_dir = wm_with_er
    frozen = await _freeze_running_er(client, workspace_dir)
    try:
        results = await asyncio.wait_for(
            client.reload_config(project=str(workspace_dir)), timeout=45.0
        )
        assert results[0]["status"] in {"recovered", "failed"}
        _wait_until_pids_gone(frozen)
    finally:
        _thaw(frozen)
