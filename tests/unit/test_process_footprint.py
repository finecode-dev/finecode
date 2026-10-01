"""The footprint walk must attribute every process to exactly one row."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from finecode.wm_server import process_footprint


def _write_proc(
    proc_root: Path,
    pid: int,
    comm: str,
    ppid: int,
    pgid: int,
    rss_kb: int,
    swap_kb: int | None = None,
) -> None:
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "stat").write_text(
        f"{pid} ({comm}) R {ppid} {pgid} 0 0 0", encoding="utf-8"
    )
    status = f"Name:\t{comm}\nVmRSS:\t   {rss_kb} kB\n"
    if swap_kb is not None:
        status += f"VmSwap:\t   {swap_kb} kB\n"
    (proc_dir / "status").write_text(status, encoding="utf-8")


def _make_tree(proc_root: Path) -> tuple[int, list[process_footprint.FootprintTarget]]:
    wm_pid = 100
    _write_proc(proc_root, 100, "wm", 1, 100, 1000, 100)
    _write_proc(proc_root, 200, "er", 100, 200, 2000, 200)
    _write_proc(proc_root, 201, "er-worker", 200, 200, 500, 50)
    _write_proc(proc_root, 202, "new-session-child", 201, 202, 300, 30)
    _write_proc(proc_root, 203, "my ) proc", 200, 200, 400, 40)
    vanished = proc_root / "204"
    vanished.mkdir(parents=True, exist_ok=True)
    (vanished / "stat").write_text("204 (gone) R 200 200 0 0 0", encoding="utf-8")
    _write_proc(proc_root, 300, "wm-child", 100, 100, 100, 10)
    _write_proc(proc_root, 400, "untracked-er", 100, 400, 700, 70)
    _write_proc(proc_root, 401, "untracked-worker", 400, 400, 150, 15)
    targets = [process_footprint.FootprintTarget(runner_id="er1", pid=200)]
    return wm_pid, targets


def test_footprint_attributes_group_ppid_and_untracked() -> None:
    """Group members, reparented children and untracked ERs must land right.

    A pgid-only walk would miss the new-session child; without untracked rows
    a leaked ER would be invisible or misattributed to the WM.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proc_root = Path(tmp)
        wm_pid, targets = _make_tree(proc_root)

        footprint = process_footprint.read_footprint(
            wm_pid, targets, platform="linux", proc_root=proc_root
        )

        er = footprint.runners["er1"]
        assert er.process_count == 4
        assert er.rss_kb == 2000 + 500 + 300 + 400
        assert er.swap_kb == 200 + 50 + 30 + 40

        assert len(footprint.untracked) == 1
        untracked = footprint.untracked[0]
        assert untracked.pid == 400
        assert untracked.process_count == 2
        assert untracked.rss_kb == 700 + 150

        wm_pids = {100, 300}
        assert footprint.wm.process_count == len(wm_pids)
        assert footprint.wm.rss_kb == 1000 + 100
        assert footprint.wm.swap_kb == 100 + 10


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
def test_footprint_smoke_current_process() -> None:
    """The walk must see the calling process on a real host.

    The fake tree proves attribution; this proves the reader works against a
    live /proc at all.
    """
    footprint = process_footprint.read_footprint(os.getpid(), [], platform=sys.platform)

    assert footprint.wm.rss_kb > 0
