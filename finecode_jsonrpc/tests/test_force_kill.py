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
