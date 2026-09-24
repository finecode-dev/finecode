"""Acceptance tests for the per-platform process snapshot.

REQUIREMENT: at a port-handshake deadline the diagnostic must name what the
spawned server was actually doing — its processes listed, "no live process" if
it already exited, or "snapshot unavailable" with a reason — on Linux, macOS
and Windows alike. POSIX processes are found through their process group (the
server is spawned with ``start_new_session=True``), Windows through the process
tree by parent pid.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from finecode_jsonrpc import _proc_snapshot


def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


def test_linux_lists_a_live_group() -> None:
    """A live, group-leading process is listed with state, cpu, rss and swap.

    The Linux line is the fullest diagnostic: if any field dropped out, a
    timeout diagnosis could not tell "working" from "blocked on I/O" or
    measure how much of the deadline a process consumed.
    """
    process = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                if psutil.Process(process.pid).status() == "sleeping":
                    break
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                break
            time.sleep(0.05)
        snapshot = _proc_snapshot.describe_spawned_processes(
            process.pid, platform="linux"
        )
    finally:
        _kill_process_tree(process.pid)

    assert snapshot.startswith(f"{process.pid} sleep state=sleeping cpu=")
    assert "rss=" in snapshot
    assert "swap=" in snapshot


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_posix_group_finds_a_reparented_orphan() -> None:
    """A member whose parent exited is still found, through the group alone.

    The shell reaps itself and the ``sleep`` is reparented, so a tree walk
    from the root pid misses it; only the pgid scan reaches it. A timeout on a
    busy child exactly this way must not read "no live process".
    """
    process = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo $!"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        sleep_pid = int(process.stdout.readline().strip())
        process.wait()  # reap the shell; the sleep is reparented after it
        started = time.perf_counter()
        snapshot = _proc_snapshot.describe_spawned_processes(process.pid)
        elapsed = time.perf_counter() - started
    finally:
        _kill_process_tree(process.pid)

    assert f"{sleep_pid} sleep" in snapshot
    # AC11: the call runs on the WM's shared IO loop, so a slow scan would
    # stall every runner in the workspace at the worst moment (a failed start).
    assert elapsed < 0.2


def test_darwin_format_has_no_swap() -> None:
    """The macOS line has no swap field (psutil does not report one there).

    A regression that emitted swap on macOS would claim memory data psutil
    never reads, so operators would weigh a field that cannot change outcome.
    """
    process = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        snapshot = _proc_snapshot.describe_spawned_processes(
            process.pid, platform="darwin"
        )
    finally:
        _kill_process_tree(process.pid)

    assert "state=" in snapshot
    assert "swap=" not in snapshot


def test_tree_mode_lists_descendants() -> None:
    """Windows tree mode lists the root and every descendant, with tree fields.

    Windows has no process group: only a parent-pid walk covers the whole
    server. The launcher python.exe is window dressing, so a snapshot limited
    to the root pid would misread the same tree as "idle launcher, nothing
    actually running".
    """
    child_source = (
        "import subprocess, sys, time;"
        " p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        " print(p.pid, flush=True); time.sleep(30)"
    )
    root = subprocess.Popen(
        [sys.executable, "-c", child_source],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )
    try:
        assert root.stdout is not None
        grandchild_pid = int(root.stdout.readline().strip())
        snapshot = _proc_snapshot.describe_spawned_processes(root.pid, platform="win32")
    finally:
        _kill_process_tree(root.pid)

    lines = snapshot.splitlines()
    assert any(line.startswith(f"{root.pid} ") for line in lines)
    assert any(line.startswith(f"{grandchild_pid} ") for line in lines)
    for line in lines:
        assert "ppid=" in line and "threads=" in line
        assert "cpu=" in line and "rss=" in line
        assert "state=" not in line


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ppid is not rewritten")
def test_tree_mode_finds_orphan_of_dead_root() -> None:
    """A surviving descendant of an exited server is still listed on Windows.

    Windows keeps the parent pid of the orphan pointing at the dead server, so
    the ppid scan re-derives the tree even after the root is gone. Linux
    reparents orphans, so this is Windows-only — AC4 covers the live-root walk
    everywhere else.
    """
    child_source = (
        "import subprocess, sys;"
        " p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        " print(p.pid, flush=True)"
    )
    root = subprocess.Popen(
        [sys.executable, "-c", child_source],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert root.stdout is not None
        grandchild_pid = int(root.stdout.readline().strip())
        root.wait()  # the child exits; the grandchild survives, reparented
        snapshot = _proc_snapshot.describe_spawned_processes(root.pid)
    finally:
        _kill_process_tree(grandchild_pid)

    assert any(line.startswith(f"{grandchild_pid} ") for line in snapshot.splitlines())


def test_empty_group() -> None:
    """An empty group reads its exact string, so "already exited" is unmissable.

    The start error interpolates this verbatim; wording changes here are
    changes to the diagnostic operators search for.
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.kill()
    process.wait()

    assert (
        _proc_snapshot.describe_spawned_processes(process.pid)
        == f"no live process in process group {process.pid}"
    )


def test_empty_tree() -> None:
    """An empty tree reads its exact string, distinct from the group one.

    The tree/group wording difference tells the operator which membership
    rule the snapshot used, so a wrong match on Windows cannot hide behind a
    correct-looking empty group.
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.kill()
    process.wait()

    assert (
        _proc_snapshot.describe_spawned_processes(process.pid, platform="win32")
        == f"no live process in the process tree of {process.pid}"
    )


def test_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot failure reads "unavailable" plus a reason, never raises.

    The snapshot explains a start error; replacing it with another exception
    (or a bare "unavailable" with no cause) would hide the original failure
    from the operator at exactly the moment they need it.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(psutil, "process_iter", boom)

    result = _proc_snapshot.describe_spawned_processes(1)

    assert result == "process snapshot unavailable: RuntimeError: boom"


def test_access_denied_field_prints_question_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field a process denies is still listed, with ``?`` for that field.

    A timeout diagnosis must see the process even when a memory read is
    refused; dropping the whole line would make "exists but unreadable"
    indistinguishable from "already exited".
    """

    def denied(self: psutil.Process) -> object:
        raise psutil.AccessDenied(self.pid)

    original = psutil.Process.memory_info
    # ``oneshot()`` calls these on the bound method to seed/clear its cache;
    # a bare function replacement would crash before any field is read.
    denied.cache_activate = original.cache_activate
    denied.cache_deactivate = original.cache_deactivate
    monkeypatch.setattr(psutil.Process, "memory_info", denied)
    process = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        snapshot = _proc_snapshot.describe_spawned_processes(
            process.pid, platform="linux"
        )
    finally:
        _kill_process_tree(process.pid)

    assert "rss=?" in snapshot
    assert f"{process.pid} sleep" in snapshot
