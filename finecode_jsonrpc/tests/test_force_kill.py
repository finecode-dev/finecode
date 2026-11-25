"""Requirement tests: force_kill() must reach a process with no live RPC channel.

REQUIREMENT: a server process can end up with no cooperative way to stop it — the
start attempt timed out before the port handshake completed, so there is no RPC
connection to send a graceful exit over. force_kill() is the only way to reclaim
that process; these tests pin its platform-specific behavior and its safety when
there is nothing to kill.
"""

from __future__ import annotations

import signal
import sys

import pytest

from finecode_jsonrpc import client as jc


async def _fake_start_server(**_kwargs) -> tuple[None, None, None, int]:
    return (None, None, None, 4242)


def _make_client() -> jc.JsonRpcClient:
    return jc.JsonRpcClient(message_types={}, readable_id="test-client")


def test_force_kill_is_a_no_op_when_process_was_never_spawned() -> None:
    """Nothing to kill before pid is known (e.g. spawn itself failed) — must not
    raise or attempt to signal an arbitrary/unset pid."""
    client = _make_client()
    assert client.pid is None

    client.force_kill()  # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_kills_the_process_group_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX, the whole process group must be signaled, not just the ER's own
    pid — it is started with start_new_session=True specifically so a package
    manager or other tool it spawns is reachable as one group here. Missing this
    would leave those children running after the ER itself is gone."""
    client = _make_client()
    client.pid = 4242
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))

    client.force_kill()

    assert killed == [(4242, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_swallows_already_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that exited on its own between the caller deciding to force-kill
    it and the signal actually being sent is not an error — the goal (no process
    left running) is already satisfied."""
    client = _make_client()
    client.pid = 4242

    def _raise(pid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr("os.killpg", _raise)

    client.force_kill()  # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_tolerates_a_group_it_may_not_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A force-kill must not fail on a group it is not permitted to signal.

    macOS answers ``killpg`` with EPERM for a group whose only members are
    unreaped zombies: the process is effectively gone, but the OS declines the
    signal rather than reporting ESRCH. Raising here would replace the real
    failure the caller is about to report (e.g. an ER start time-out) with this
    EPERM, hiding the failure the operator needs to see.
    """
    client = _make_client()
    client.pid = 4242

    def _raise(pid: int, sig: int) -> None:
        raise PermissionError()

    monkeypatch.setattr("os.killpg", _raise)

    client.force_kill()  # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
async def test_force_kill_before_pid_is_known_still_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start cancellation can land between the OS spawn and the pid being
    recorded on the main loop. A force_kill() in that window must still reach
    the process once the pid is known, or the start leaks an ER process."""
    client = _make_client()
    monkeypatch.setattr(jc, "start_server", _fake_start_server)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))

    client.force_kill()
    await client._spawn_and_record()

    assert killed == [(4242, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
async def test_spawn_records_pid_without_killing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common path must record the pid as soon as the process exists and
    must not signal anything when no kill was requested."""
    client = _make_client()
    monkeypatch.setattr(jc, "start_server", _fake_start_server)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))

    await client._spawn_and_record()

    assert killed == []
    assert client.pid == 4242




def _session_sleeper_script() -> str:
    return "import time; time.sleep(300)"


def _spawn_server_with_two_owned_children():
    """A stand-in server: its own session, holding two own-session children."""
    import subprocess
    import sys as _sys

    script = (
        "import subprocess, sys, time\n"
        "for _ in range(2):\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],\n"
        "                     start_new_session=True)\n"
        "time.sleep(300)\n"
    )
    return subprocess.Popen(
        [_sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_gone(pid: int, timeout: float = 10.0) -> None:
    import os
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} survived")


def _wait_alive(pid: int, timeout: float = 10.0) -> None:
    import os
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            time.sleep(0.05)
            continue
        return
    raise AssertionError(f"pid {pid} never appeared")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_with_descendant_groups_kills_owned_children() -> None:
    """With the flag, own-session children die with the server's group."""
    import os
    import time

    from finecode_jsonrpc import _proc_kill as proc_kill

    server = _spawn_server_with_two_owned_children()
    try:
        time.sleep(0.5)
        descendants = proc_kill._snapshot_descendant_groups(server.pid)
        assert len(descendants) == 2
        proc_kill.force_kill_process_group(
            server.pid, "test", descendant_groups=True
        )
        assert server.wait(timeout=10) == -signal.SIGKILL
        for pgid in descendants:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(f"descendant group {pgid} survived")
    finally:
        try:
            server.kill()
        except Exception:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_without_flag_leaves_descendant_groups() -> None:
    """The default keeps today's behaviour: other sessions survive."""
    import os
    import time

    from finecode_jsonrpc import _proc_kill as proc_kill

    server = _spawn_server_with_two_owned_children()
    try:
        time.sleep(0.5)
        descendants = proc_kill._snapshot_descendant_groups(server.pid)
        assert len(descendants) == 2
        proc_kill.force_kill_process_group(server.pid, "test")
        assert server.wait(timeout=10) == -signal.SIGKILL
        for pgid in descendants:
            os.killpg(pgid, 0)
    finally:
        for pgid in descendants:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            server.kill()
        except Exception:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_reaped_leader_orphan_survives(tmp_path) -> None:
    """A descendant whose leader was already reaped escapes the snapshot."""
    import os
    import subprocess
    import sys as _sys
    import time

    from finecode_jsonrpc import _proc_kill as proc_kill

    pid_file = tmp_path / "orphan.pid"
    grandchild_code = (
        "import os, time; os.setsid(); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(300)"
    )
    middle_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}], "
        "start_new_session=False)"
    )
    server_code = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', {middle_code!r}]); "
        "p.wait(); time.sleep(300)"
    )
    server = subprocess.Popen(
        [_sys.executable, "-c", server_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    orphan_pid: int | None = None
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if pid_file.exists():
                orphan_pid = int(pid_file.read_text(encoding="utf-8"))
                break
            time.sleep(0.05)
        assert orphan_pid is not None
        _wait_alive(orphan_pid)
        proc_kill.force_kill_process_group(
            server.pid, "test", descendant_groups=True
        )
        time.sleep(0.3)
        os.kill(orphan_pid, 0)
        psutil = pytest.importorskip("psutil")
        assert psutil.Process(orphan_pid).status() != psutil.STATUS_ZOMBIE
    finally:
        if orphan_pid is not None:
            try:
                os.kill(orphan_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            server.kill()
        except Exception:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_skips_own_and_reserved_pgids_and_tolerates_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reserved pgids are never signalled; a vanished group raises nothing."""
    import os

    from finecode_jsonrpc import _proc_kill as proc_kill

    real_killpg = os.killpg
    signalled: list[int] = []

    def _recording(pid: int, sig: int) -> None:
        if pid == 4242:
            raise ProcessLookupError()
        signalled.append(pid)
        return real_killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", _recording)
    monkeypatch.setattr(
        proc_kill,
        "_snapshot_descendant_groups",
        lambda pid: [4242, os.getpgrp(), 0, 1],
    )
    proc_kill.force_kill_process_group(4242, "test", descendant_groups=True)
    assert signalled == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill path")
def test_force_kill_group_spawned_after_snapshot_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group born after the snapshot is residue, not a kill failure."""
    import subprocess
    import sys as _sys
    import time

    from finecode_jsonrpc import _proc_kill as proc_kill

    server = _spawn_server_with_two_owned_children()
    late = None
    try:
        time.sleep(0.5)
        real_snapshot = proc_kill._snapshot_descendant_groups

        def _snapshot_then_fork(pid: int):
            groups = real_snapshot(pid)
            late_proc = subprocess.Popen(
                [_sys.executable, "-c", "import time; time.sleep(300)"],
                start_new_session=True,
            )
            nonlocal late
            late = late_proc
            return groups

        monkeypatch.setattr(
            proc_kill, "_snapshot_descendant_groups", _snapshot_then_fork
        )
        proc_kill.force_kill_process_group(
            server.pid, "test", descendant_groups=True
        )
        assert late is not None
        _wait_alive(late.pid)
    finally:
        if late is not None:
            late.kill()
            late.wait()
        try:
            server.kill()
        except Exception:
            pass
