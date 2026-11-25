"""AsyncProcess internals: leader exit, group-gone and strict output.

Why it matters: scope exit must be keyed to the process or group ending, not
to pipes closing, or a child holding a pipe open keeps a slot held forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

import pytest

from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_runner.impls.command_runner import (
    AsyncProcess,
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.owned_commands import OwnedCommandRegistry
from finecode_extension_runner.process_slots import ProcessSlots

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


async def _spawn(asyncio_subprocess_kwargs, *, owns_group: bool, strict: bool) -> AsyncProcess:
    proc = await asyncio.create_subprocess_exec(*asyncio_subprocess_kwargs)
    return AsyncProcess(proc, owns_process_group=owns_group, strict_output=strict)


async def test_leader_exit_observed_before_pipe_close() -> None:
    """Exit is reported even while a group member holds stdout open.

    A `setsid` holder inherits stdout and sleeps; the leader exits at once.
    Keying release on `Process.wait()` would wait for the holder instead.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os, subprocess, sys; subprocess.Popen([sys.executable, '-c',"
        " 'import os, time; os.setsid(); time.sleep(30)'], stdout=sys.stdout); ",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    wrapped = AsyncProcess(proc, owns_process_group=True, strict_output=True)
    try:
        await asyncio.wait_for(wrapped.wait_for_leader_exit(), timeout=5.0)
        assert proc.returncode is not None
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        wrapped.abandon_drain()
        await asyncio.sleep(0.1)


async def test_zombie_only_group_counts_as_gone(monkeypatch) -> None:
    """A group whose members are all zombies needs no further signal."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    wrapped = AsyncProcess(proc, owns_process_group=True)
    await proc.wait()
    assert wrapped.group_survivors() == ()
    assert wrapped.is_alive() is False


async def test_eperm_group_counts_as_live(monkeypatch) -> None:
    """A group we cannot signal still counts as alive, never as gone."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    wrapped = AsyncProcess(proc, owns_process_group=True)
    try:
        real_killpg = os.killpg

        def fake_killpg(pgid, sig):
            if pgid == proc.pid:
                raise PermissionError("denied")
            return real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", fake_killpg)
        assert wrapped.is_alive() is True
        assert wrapped.group_survivors() != ()
    finally:
        monkeypatch.undo()
        proc.kill()
        await proc.wait()
        wrapped.abandon_drain()


async def test_gone_latch_never_probes_again(monkeypatch) -> None:
    """Once gone, the group id is never signalled again (pid reuse guard)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    wrapped = AsyncProcess(proc, owns_process_group=True)
    await proc.wait()
    assert wrapped.group_survivors() == ()
    calls: list[int] = []
    real_killpg = os.killpg

    def recording_killpg(pgid, sig):
        calls.append(pgid)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    try:
        assert wrapped.is_alive() is False
        wrapped.terminate()
        wrapped.kill()
        assert wrapped.group_survivors() == ()
        assert calls == []
    finally:
        monkeypatch.undo()
        wrapped.abandon_drain()


async def test_strict_output_raises_after_drain_failure() -> None:
    """An overlong line makes strict readers raise instead of serving a prefix."""
    strict_proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdout.write('x' * (9 * 1024 * 1024))",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    strict = AsyncProcess(strict_proc, strict_output=True)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(strict.wait_for_end(), timeout=10.0)
    with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
        strict.get_output()

    lax_proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdout.write('x' * (9 * 1024 * 1024))",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    lax = AsyncProcess(lax_proc, strict_output=False)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(lax.wait_for_end(), timeout=10.0)
    assert isinstance(lax.get_output(), str)


class _NoopLogger:
    def debug(self, message: str) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def exception(self, exception: Exception) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


class _Backend:
    """Fake WM budget with controllable lease/release gates."""

    def __init__(self) -> None:
        self.lease_hold: asyncio.Event | None = None
        self.release_hold: asyncio.Event | None = None
        self._issued = 0
        self.outstanding: set[str] = set()

    async def lease(self) -> str:
        if self.lease_hold is not None:
            await self.lease_hold.wait()
        self._issued += 1
        lease_id = f"L{self._issued}"
        self.outstanding.add(lease_id)
        return lease_id

    async def release(self, lease_id: str) -> None:
        if self.release_hold is not None:
            await self.release_hold.wait()
        self.outstanding.discard(lease_id)


def _make_runner(
    target: int = 4, local_cap: int | None = 4, backend: _Backend | None = None
) -> tuple[CommandRunner, ProcessSlots, _Backend, OwnedCommandRegistry]:
    slots = ProcessSlots(target=target)
    be = backend if backend is not None else _Backend()
    slots.attach_budget(be.lease, be.release)
    registry = OwnedCommandRegistry()
    runner = CommandRunner(
        logger=_NoopLogger(),
        config=CommandRunnerConfig(max_concurrent_processes=local_cap),
        process_slots=slots,
        owned_commands=registry,
    )
    return runner, slots, be, registry


async def _assert_fully_released(
    runner: CommandRunner, slots: ProcessSlots, backend: _Backend, cap_before: int
) -> None:
    assert slots.in_flight == 0
    assert backend.outstanding == set()
    if runner._local_cap is not None:
        assert runner._local_cap._value == cap_before


async def test_scope_exit_releases_everything_on_every_path(tmp_path) -> None:
    """Every exit path reaps the child and returns the slot, cap and lease."""

    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    watched = tmp_path / "watched.txt"
    watched.write_text("stay", encoding="utf-8")
    quick = [
        sys.executable,
        "-c",
        "import sys; open(sys.argv[1]).read(); print('hi')",
        str(watched),
    ]
    sleepy = [sys.executable, "-c", "import time; time.sleep(30)"]

    async def check(proc: AsyncProcess) -> None:
        assert proc.get_exit_code() is not None
        assert not proc.is_alive()
        await asyncio.wait_for(proc.wait_for_end(), timeout=1.0)
        assert watched.exists()
        await _assert_fully_released(runner, slots, backend, cap_before)

    proc = await runner.run_to_completion(quick)
    assert proc.get_output() == "hi\n"
    await check(proc)

    async def early() -> str:
        async with runner.run(quick, cwd=None, env=None, new_process_group=True) as proc:
            early_proc["p"] = proc
            return "early"

    early_proc: dict[str, AsyncProcess] = {}
    assert await early() == "early"
    await check(early_proc["p"])

    async def boom() -> None:
        async with runner.run(quick, cwd=None, env=None, new_process_group=True):
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await boom()
    assert slots.in_flight == 0
    assert backend.outstanding == set()

    async def under_timeout() -> None:
        async with asyncio.timeout(0.2):
            async with runner.run(sleepy, cwd=None, env=None, new_process_group=True) as proc:
                timeout_proc["p"] = proc
                await asyncio.sleep(30)

    timeout_proc: dict[str, AsyncProcess] = {}
    with pytest.raises(TimeoutError):
        await under_timeout()
    await check(timeout_proc["p"])

    with pytest.raises(TimeoutError):
        await runner.run_to_completion(sleepy, timeout=0.2)
    await _assert_fully_released(runner, slots, backend, cap_before)

    async def holder() -> None:
        async with runner.run(sleepy, cwd=None, env=None, new_process_group=True) as proc:
            cancelled_proc["p"] = proc
            await asyncio.sleep(30)

    cancelled_proc: dict[str, AsyncProcess] = {}
    task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await check(cancelled_proc["p"])


async def test_buffered_command_with_outputs_and_stdin() -> None:
    """Exit codes, streams and stdin handling through the buffered call."""
    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value

    for code in (0, 1, 2):
        proc = await runner.run_to_completion(
            [sys.executable, "-c", f"import sys; sys.stdout.write('o{code}'); sys.stderr.write('e{code}'); sys.exit({code})"]
        )
        assert proc.get_exit_code() == code
        assert proc.get_output() == f"o{code}"
        assert proc.get_error_output() == f"e{code}"


    async with runner.run([sys.executable, "-c", "print('l1'); print('l2')"], cwd=None, env=None, new_process_group=True) as proc:
        assert [line async for line in proc.stdout_lines()] == ["l1", "l2"]
    assert proc.get_exit_code() == 0

    async with runner.run([sys.executable, "-c", "import time; time.sleep(5)"], cwd=None, env=None, new_process_group=True) as proc:
        with pytest.raises(TimeoutError):
            await proc.wait_for_end(timeout=0.1)
        assert proc.is_alive()
    assert not proc.is_alive()

    echo = await runner.run_to_completion(
        [sys.executable, "-c", "import sys; data = sys.stdin.read(); sys.stdout.write(data)"],
        input="x",
    )
    assert echo.get_output() == "x"

    eof = await runner.run_to_completion(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
    )
    assert eof.get_output() == ""
    await _assert_fully_released(runner, slots, backend, cap_before)


async def test_process_group_default_and_opt_out() -> None:
    """Owned commands run in their own session unless opted out."""
    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    child = [sys.executable, "-c", "import os; print(os.getsid(0))"]

    proc = await runner.run_to_completion(child)
    assert proc.get_output().strip() == str(proc.async_subprocess.pid)


    async with runner.run(child, cwd=None, env=None, new_process_group=False) as plain:
        await plain.wait_for_end()
    assert plain.get_output().strip() == str(os.getsid(0))

    long_proc = await runner.start_long_running(child)
    try:
        assert slots.in_flight == 0
        await asyncio.wait_for(long_proc.wait_for_end(), timeout=10.0)
        assert long_proc.get_output().strip() == str(os.getsid(0))
    finally:
        if long_proc.is_alive():
            long_proc.kill()
            await long_proc.wait_for_end()
    await _assert_fully_released(runner, slots, backend, cap_before)


async def test_entry_refusals_raise_before_spawn() -> None:
    """Refusals and spawn errors hold no slot, cap or lease and await no WM."""

    backend = _Backend()
    backend.release_hold = asyncio.Event()
    backend.release_hold.set()
    runner, slots, backend, _ = _make_runner(backend=backend)
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    backend.lease_hold = asyncio.Event()
    backend.lease_hold.set()

    async def attempt(cmd: list[str]) -> None:
        async with runner.run(cmd, cwd=None, env=None, new_process_group=True):
            pass  # pragma: no cover

    with pytest.raises(TypeError):
        await attempt("git status")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await attempt([])
    with pytest.raises(icommandrunner.ProgramStartError):
        await attempt(["definitely-not-a-real-program-xyz", "x"])
    assert slots.in_flight == 0
    assert runner._local_cap._value == cap_before
    for _ in range(50):
        await asyncio.sleep(0)
        if backend.outstanding == set():
            break
    assert backend.outstanding == set()


async def test_entry_refusals_on_windows_shape(monkeypatch) -> None:
    """Batch-unsafe and unlaunchable programs are refused before any spawn."""

    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "shutil.which", lambda prog, path=None: "C:\\tools\\npm.cmd"
    )

    async def attempt(cmd: list[str]) -> None:
        async with runner.run(cmd, cwd=None, env=None, new_process_group=True):
            pass  # pragma: no cover

    with pytest.raises(icommandrunner.UnsafeBatchArgumentError):
        await attempt(["npm", "a&b"])
    monkeypatch.setattr(
        "shutil.which", lambda prog, path=None: "C:\\tools\\prog.js"
    )
    with pytest.raises(icommandrunner.UnlaunchableProgramError):
        await attempt(["prog", "x"])
    assert slots.in_flight == 0
    assert runner._local_cap._value == cap_before
    assert backend.outstanding == set()


async def test_entry_cancel_while_waiting_for_slot() -> None:
    """Cancelling a slot wait holds nothing and awaits no WM round trip."""

    runner, slots, backend, _ = _make_runner(target=1, local_cap=4)
    assert runner._local_cap is not None
    async with runner.run([sys.executable, "-c", "import time; time.sleep(30)"]):
        assert slots.in_flight == 1

        async def entry() -> None:
            async with runner.run([sys.executable, "-c", "pass"], cwd=None, env=None, new_process_group=True):
                pass  # pragma: no cover

        task = asyncio.ensure_future(entry())
        await asyncio.sleep(0.1)
        assert slots.in_flight == 1
        loop = asyncio.get_running_loop()
        start = loop.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert loop.time() - start < 0.5
        assert slots.in_flight == 1
        assert backend.outstanding == {"L1"}


async def test_entry_cancel_while_waiting_for_lease() -> None:
    """A lease granted after propagation is still released."""

    backend = _Backend()
    backend.lease_hold = asyncio.Event()
    runner, slots, backend, _ = _make_runner(backend=backend)
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value

    async def entry() -> None:
        async with runner.run([sys.executable, "-c", "pass"], cwd=None, env=None, new_process_group=True):
            pass  # pragma: no cover

    task = asyncio.ensure_future(entry())
    await asyncio.sleep(0.1)
    assert slots.in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.in_flight == 0
    assert runner._local_cap._value == cap_before
    backend.lease_hold.set()
    for _ in range(50):
        await asyncio.sleep(0)
        if backend.outstanding == set():
            break
    assert backend.outstanding == set()


async def test_entry_cancel_while_waiting_for_local_cap() -> None:
    """Cancelling a cap wait returns the slot without awaiting the WM."""

    runner, slots, backend, _ = _make_runner(target=4, local_cap=1)
    assert runner._local_cap is not None
    async with runner.run([sys.executable, "-c", "import time; time.sleep(30)"]):
        leases_before = set(backend.outstanding)

        async def entry() -> None:
            async with runner.run([sys.executable, "-c", "pass"], cwd=None, env=None, new_process_group=True):
                pass  # pragma: no cover

        task = asyncio.ensure_future(entry())
        await asyncio.sleep(0.1)
        assert len(backend.outstanding) == len(leases_before) + 1
        loop = asyncio.get_running_loop()
        start = loop.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert loop.time() - start < 0.5
        assert backend.outstanding == leases_before
    for _ in range(50):
        await asyncio.sleep(0)
        if slots.in_flight == 0:
            break
    assert slots.in_flight == 0


async def test_scope_inside_work_slot_scope_is_refused() -> None:
    """Opening a scope inside IWorkSlots.acquire() raises WorkSlotScopeError."""
    from finecode_extension_api.interfaces.iworkslots import WorkSlotScopeError
    from finecode_extension_runner.work_slots import WorkSlots

    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    async with WorkSlots(slots).acquire():
        with pytest.raises(WorkSlotScopeError):
            async with runner.run([sys.executable, "-c", "pass"], cwd=None, env=None, new_process_group=True):
                pass  # pragma: no cover
    assert slots.in_flight == 0
    assert runner._local_cap._value == cap_before
    assert backend.outstanding == set()


async def test_scope_from_foreign_loop_is_refused() -> None:
    """A scope bound to another loop raises at entry, before any spawn."""

    runner, slots, backend, registry = _make_runner()
    other: asyncio.AbstractEventLoop = asyncio.new_event_loop()
    try:
        registry.bind_loop(other)
        with pytest.raises(RuntimeError):
            async with runner.run([sys.executable, "-c", "pass"], cwd=None, env=None, new_process_group=True):
                pass  # pragma: no cover
    finally:
        other.close()
    assert slots.in_flight == 0
    assert backend.outstanding == set()


async def test_cancel_during_spawn_sweep(tmp_path) -> None:
    """Cancels across the spawn boundary leave no grandchild and no slot."""

    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    ready = tmp_path / "ready"
    child = ["sh", "-c", f"sleep 30 & echo $! > {ready}; exec sleep 30"]
    iterations = 200
    durations: list[float] = []
    loop = asyncio.get_running_loop()

    async def entry() -> None:
        async with runner.run(child, cwd=None, env=None, new_process_group=True):
            await asyncio.sleep(30)

    for i in range(iterations):
        if ready.exists():
            ready.unlink()
        task: asyncio.Task[None] = asyncio.ensure_future(entry())
        if i % 2 == 0:
            task.cancel()
        else:
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(0.005)
            task.cancel()
        start = loop.time()
        with pytest.raises(asyncio.CancelledError):
            await task
        durations.append(loop.time() - start)
        assert slots.in_flight == 0
        if ready.exists():
            grandchild = int(ready.read_text(encoding="utf-8").strip())
            deadline = loop.time() + 2.0
            while loop.time() < deadline:
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"grandchild {grandchild} survived (iteration {i})")

    assert max(durations) < 4.5
    assert backend.outstanding == set()
    assert runner._local_cap._value == cap_before
    results = tmp_path / "sweep.json"
    results.write_text(
        json.dumps(
            {"iterations": iterations, "max": max(durations), "p50": sorted(durations)[len(durations) // 2]}
        ),
        encoding="utf-8",
    )


async def test_cancel_during_spawn_without_group_stops_leader(tmp_path) -> None:
    """Without a group only the leader is promised stopped."""

    runner, slots, backend, _ = _make_runner()
    ready = tmp_path / "ready"
    child = ["sh", "-c", f"sleep 30 & echo $! > {ready}; exec sleep 30"]
    async with runner.run(child, cwd=None, env=None, new_process_group=False) as proc:
        for _ in range(200):
            if ready.exists():
                break
            await asyncio.sleep(0.005)
        assert proc.async_subprocess.returncode is None
    assert proc.async_subprocess.returncode is not None
    grandchild_pid = int(ready.read_text(encoding="utf-8").strip())
    try:
        os.kill(grandchild_pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(grandchild_pid, 9)
    assert slots.in_flight == 0


async def test_exit_with_pipe_holder_abandons_drain(tmp_path) -> None:
    """A setsid holder keeps stdout open; exit is bounded by the drain wait."""
    from finecode_extension_runner.impls.command_runner import _DRAIN_WAIT_SEC

    runner, slots, backend, _ = _make_runner()
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    marker = f"owned-hold-{os.getpid()}"
    script = tmp_path / "leader.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        f"marker = {marker!r}\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "                  'import os, time; os.setsid(); time.sleep(30)  # ' + marker],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)\n"
        "time.sleep(0.5)\n",
        encoding="utf-8",
    )
    child = [sys.executable, str(script)]
    loop = asyncio.get_running_loop()
    async with runner.run(child, cwd=None, env=None, new_process_group=True) as proc:
        await proc.wait_for_leader_exit()
        start = loop.time()
    elapsed = loop.time() - start
    assert elapsed <= _DRAIN_WAIT_SEC + 0.5
    assert slots.in_flight == 0
    assert backend.outstanding == set()
    assert runner._local_cap._value == cap_before
    psutil = pytest.importorskip("psutil")
    holder_pid: int | None = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        found = False
        for p in psutil.process_iter(["pid", "cmdline"]):
            if marker in " ".join(p.info["cmdline"] or []):
                holder_pid = p.info["pid"]
                found = True
        if found:
            break
    assert holder_pid is not None
    os.kill(holder_pid, 9)
    with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
        proc.get_output()
    with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
        await proc.wait_for_end()


async def test_overlong_line_is_strict_in_scope_but_prefix_for_long_running() -> None:
    """Drain failures: strict readers raise incomplete; long-running keeps prefix."""

    runner, slots, _, _ = _make_runner()
    child = [
        sys.executable, "-c", "import sys; sys.stdout.write('x' * (9 * 1024 * 1024))",
    ]
    async with runner.run(child, cwd=None, env=None, new_process_group=True) as proc:
        with pytest.raises(RuntimeError):
            await proc.wait_for_end()
    with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
        proc.get_output()
    assert slots.in_flight == 0

    with pytest.raises(RuntimeError):
        await runner.run_to_completion(child)
    assert slots.in_flight == 0

    long_proc = await runner.start_long_running(child)
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(long_proc.wait_for_end(), timeout=10.0)
        assert isinstance(long_proc.get_output(), str)
    finally:
        if long_proc.is_alive():
            long_proc.kill()
        with contextlib.suppress(RuntimeError):
            await long_proc.wait_for_end()


async def test_unstoppable_group_raises_and_holds_slot(monkeypatch) -> None:
    """A persistently EPERM group raises with pids; the slot stays held."""

    runner, slots, _, _ = _make_runner()

    def deny_all(pgid: int, sig: int) -> None:
        raise PermissionError("denied")

    async def settled_in_flight(target: int, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while slots.in_flight != target:
            if loop.time() >= deadline:
                raise TimeoutError(f"in_flight stayed {slots.in_flight}")
            await asyncio.sleep(0.05)

    monkeypatch.setattr(os, "killpg", deny_all)
    with pytest.raises(icommandrunner.OwnedProcessNotStoppedError) as caught:
        async with runner.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=None, env=None, new_process_group=True):
            await asyncio.sleep(0.2)
    assert caught.value.pids != ()
    assert str(caught.value.pids[0]) in str(caught.value)
    assert slots.in_flight == 1
    monkeypatch.undo()
    await settled_in_flight(0)

    monkeypatch.setattr(os, "killpg", deny_all)
    try:
        async with runner.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=None, env=None, new_process_group=True):
            raise ValueError("original")
    except ValueError as error:
        assert any("still alive" in note for note in getattr(error, "__notes__", []))
    else:
        raise AssertionError("ValueError did not propagate")
    assert slots.in_flight == 1
    monkeypatch.undo()
    await settled_in_flight(0)


async def test_close_all_logs_pending_background_work() -> None:
    """Background work still pending at ER stop is logged, not awaited."""
    from loguru import logger

    registry = OwnedCommandRegistry()

    async def never() -> None:
        await asyncio.sleep(30)

    messages: list[str] = []
    task: asyncio.Task[None] = asyncio.ensure_future(never())
    sink = logger.add(messages.append, level="WARNING")
    try:
        registry.keep(task, "test-watcher")
        await registry.close_all()
        assert any("still pending" in message for message in messages)
    finally:
        logger.remove(sink)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_unanswered_wm_release_bounds_exit_but_holds_slot() -> None:
    """An unanswered WM release bounds the exit; the slot frees when it answers."""
    from loguru import logger

    from finecode_extension_runner.impls.command_runner import _RELEASE_WAIT_SEC

    backend = _Backend()
    backend.release_hold = asyncio.Event()
    runner, slots, backend, _ = _make_runner(backend=backend)
    assert runner._local_cap is not None
    cap_before = runner._local_cap._value
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        proc = await runner.run_to_completion(
            [sys.executable, "-c", "print('hi')"]
        )
        elapsed = loop.time() - start
        assert proc.get_output() == "hi\n"
        assert _RELEASE_WAIT_SEC <= elapsed <= _RELEASE_WAIT_SEC + 1.0
        assert any("in flight" in message for message in messages)
        assert slots.in_flight == 1
        assert runner._local_cap._value == cap_before
        backend.release_hold.set()
        deadline = loop.time() + 5.0
        while slots.in_flight != 0:
            if loop.time() >= deadline:
                raise TimeoutError("slot never released")
            await asyncio.sleep(0.05)
    finally:
        logger.remove(sink)


async def test_scope_is_single_use() -> None:
    """Entering the same scope twice raises instead of spawning twice."""
    runner, slots, _, _ = _make_runner()
    scope = runner.run([sys.executable, "-c", "pass"])
    async with scope:
        pass
    with pytest.raises(RuntimeError, match="only be entered once"):
        async with scope:
            pass  # pragma: no cover
    assert slots.in_flight == 0


async def test_registry_register_complete_bookkeeping() -> None:
    """Register tracks the holder; complete releases it and resolves done."""
    from finecode_extension_runner.owned_commands import OwnedCommandRegistry

    registry = OwnedCommandRegistry()
    holder: asyncio.Task[object] = asyncio.ensure_future(asyncio.sleep(30))
    try:
        entry = registry.register(holder)
        assert registry.open_count == 1
        assert entry.pid is None
        assert entry.holder is holder
        registry.complete(entry)
        assert registry.open_count == 0
        await asyncio.wait_for(asyncio.wrap_future(entry.done), timeout=1.0)
    finally:
        holder.cancel()
        try:
            await holder
        except asyncio.CancelledError:
            pass


async def _wait_for_group(proc: AsyncProcess, count: int, timeout: float = 5.0) -> None:
    """Wait until the owned group has `count` live members (trap installed)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(proc.group_survivors()) < count:
        if loop.time() >= deadline:
            raise TimeoutError("owned group never reached readiness")
        await asyncio.sleep(0.05)


async def test_group_stop_escalates_term_then_kill() -> None:
    """A group ignoring SIGTERM gets it first, then SIGKILL after the grace."""
    from finecode_extension_runner.impls.command_runner import (
        _GONE_WAIT_SEC,
        _STOP_GRACE_SEC,
    )

    runner, slots, backend, _ = _make_runner()
    ignore = ["sh", "-c", 'trap "" TERM; sleep 30 & wait']
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with runner.run(ignore, cwd=None, env=None, new_process_group=True) as proc:
        await _wait_for_group(proc, 2)
    elapsed = loop.time() - start
    assert elapsed >= _STOP_GRACE_SEC
    assert elapsed <= _STOP_GRACE_SEC + _GONE_WAIT_SEC + 2.5
    assert slots.in_flight == 0


async def test_leader_exiting_in_grace_is_never_sigkilled(tmp_path) -> None:
    """A leader that leaves on SIGTERM is not followed by SIGKILL."""
    from finecode_extension_runner.impls.command_runner import _STOP_GRACE_SEC

    runner, slots, _, _ = _make_runner()
    mark = tmp_path / "sig.txt"
    child = [
        sys.executable,
        "-c",
        "import os, signal, time; signal.signal(signal.SIGTERM,"
        " lambda s, f: (open(r'" + str(mark) + "', 'w').write('TERM'), os._exit(0)));"
        " time.sleep(30)",
    ]
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with runner.run(child, cwd=None, env=None, new_process_group=True):
        await asyncio.sleep(0.5)
    elapsed = loop.time() - start
    assert elapsed < _STOP_GRACE_SEC + 0.5
    assert mark.read_text(encoding="utf-8") == "TERM"
    assert slots.in_flight == 0


async def test_exited_leader_with_live_member_is_stopped() -> None:
    """A group whose leader already exited is still stopped and confirmed gone."""

    runner, slots, _, _ = _make_runner()
    async with runner.run(["sh", "-c", "sleep 30 &"], cwd=None, env=None, new_process_group=True) as proc:
        await asyncio.sleep(0.3)
        assert proc.get_exit_code() is not None
        assert proc.is_alive()
    assert not proc.is_alive()
    assert proc.group_survivors() == ()
    assert slots.in_flight == 0


async def test_cancellations_during_cleanup_are_neither_lost_nor_shortening() -> None:
    """Five cancels during a 2 s cleanup yield one CancelledError with context."""
    from finecode_extension_runner.impls.command_runner import _STOP_GRACE_SEC

    runner, slots, _, _ = _make_runner()

    async def holder() -> None:
        async with runner.run(["sh", "-c", 'trap "" TERM; sleep 30 & wait'], cwd=None, env=None, new_process_group=True) as proc:
            await _wait_for_group(proc, 2)
            raise ValueError("boom")

    loop = asyncio.get_running_loop()
    task: asyncio.Task[None] = asyncio.ensure_future(holder())
    await asyncio.sleep(0.2)
    start = loop.time()
    for _ in range(5):
        await asyncio.sleep(0.05)
        task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    elapsed = loop.time() - start
    assert type(caught.value) is asyncio.CancelledError
    assert isinstance(caught.value.__context__, ValueError)
    assert task.cancelling() > 0
    assert elapsed >= _STOP_GRACE_SEC - 0.2
    assert slots.in_flight == 0


async def test_registry_check_loop_only_when_bound_and_different() -> None:
    """Unbound registries allow any loop; bound ones pin to the ER loop."""
    from finecode_extension_runner.owned_commands import OwnedCommandRegistry

    registry = OwnedCommandRegistry()
    registry.check_loop()
    registry.bind_loop(asyncio.get_running_loop())
    registry.check_loop()

    other: asyncio.AbstractEventLoop = asyncio.new_event_loop()
    try:
        registry.bind_loop(other)
        with pytest.raises(RuntimeError):
            registry.check_loop()
    finally:
        other.close()
        registry.bind_loop(asyncio.get_running_loop())


async def test_abandoned_wait_raises_incomplete_not_cancelled() -> None:
    """After abandonment, `wait_for_end` reports incompleteness, never cancellation."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    wrapped = AsyncProcess(proc, strict_output=True)
    try:
        wrapped.abandon_drain()
        with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
            await wrapped.wait_for_end()
        with pytest.raises(icommandrunner.ProcessOutputIncompleteError):
            wrapped.get_output()
    finally:
        proc.kill()
        await proc.wait()
