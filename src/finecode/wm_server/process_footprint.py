"""Per-process memory footprint of the WM and its ER trees.

One Linux ``/proc`` pass attributes every process to the WM, one ER target,
or an untracked ER that outlived its runner object. Off Linux the same shapes
are built from ``psutil`` without process groups or swap.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import psutil  # type: ignore[import-untyped]

__all__ = [
    "Footprint",
    "FootprintTarget",
    "TreeFootprint",
    "read_footprint",
]


@dataclasses.dataclass(frozen=True)
class FootprintTarget:
    runner_id: str
    pid: int


@dataclasses.dataclass(frozen=True)
class TreeFootprint:
    pid: int
    process_count: int
    rss_kb: int
    swap_kb: int | None


@dataclasses.dataclass(frozen=True)
class Footprint:
    wm: TreeFootprint
    runners: dict[str, TreeFootprint]
    untracked: list[TreeFootprint]


def read_footprint(
    wm_pid: int,
    targets: list[FootprintTarget],
    *,
    platform: str = sys.platform,
    proc_root: Path = Path("/proc"),
) -> Footprint:
    if platform.startswith("linux"):
        return _read_linux(wm_pid, targets, proc_root=proc_root)
    return _read_psutil(wm_pid, targets, platform=platform)


def _read_linux(
    wm_pid: int, targets: list[FootprintTarget], *, proc_root: Path
) -> Footprint:
    ppid_by_pid: dict[int, int] = {}
    pgid_by_pid: dict[int, int] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        close = stat_text.rfind(")")
        if close == -1:
            continue
        after = stat_text[close + 1 :].split()
        if len(after) < 3:
            continue
        try:
            ppid = int(after[1])
            pgid = int(after[2])
        except ValueError:
            continue
        ppid_by_pid[pid] = ppid
        pgid_by_pid[pid] = pgid

    children: dict[int, list[int]] = {}
    for pid, ppid in ppid_by_pid.items():
        children.setdefault(ppid, []).append(pid)

    def _close(seed: set[int], claimed: set[int]) -> set[int]:
        members: set[int] = set()
        queue = [pid for pid in seed if pid not in claimed]
        while queue:
            pid = queue.pop()
            if pid in claimed or pid in members:
                continue
            if pid not in ppid_by_pid and pid not in pgid_by_pid:
                continue
            members.add(pid)
            queue.extend(
                child
                for child in children.get(pid, [])
                if child not in claimed and child not in members
            )
        return members

    def _summarize(root: int, members: set[int]) -> TreeFootprint:
        rss = 0
        swap = 0
        count = 0
        for pid in members:
            try:
                status_text = (proc_root / str(pid) / "status").read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
            rss_kb = 0
            swap_kb = 0
            for line in status_text.splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            rss_kb = int(parts[1])
                        except ValueError:
                            rss_kb = 0
                elif line.startswith("VmSwap:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            swap_kb = int(parts[1])
                        except ValueError:
                            swap_kb = 0
            rss += rss_kb
            swap += swap_kb
            count += 1
        return TreeFootprint(pid=root, process_count=count, rss_kb=rss, swap_kb=swap)

    claimed: set[int] = set()
    target_pids = {target.pid for target in targets}
    runners: dict[str, TreeFootprint] = {}
    for target in targets:
        seed = {target.pid}
        for pid, pgid in pgid_by_pid.items():
            if pgid == target.pid:
                seed.add(pid)
        members = _close(seed, claimed)
        claimed.update(members)
        runners[target.runner_id] = _summarize(target.pid, members)

    untracked_roots: list[int] = []
    for pid, ppid in ppid_by_pid.items():
        if ppid != wm_pid:
            continue
        if pid in target_pids or pid in claimed:
            continue
        if pgid_by_pid.get(pid) != pid:
            continue
        untracked_roots.append(pid)
    untracked_roots.sort()
    untracked: list[TreeFootprint] = []
    for root in untracked_roots:
        seed = {root}
        for pid, pgid in pgid_by_pid.items():
            if pgid == root:
                seed.add(pid)
        members = _close(seed, claimed)
        claimed.update(members)
        untracked.append(_summarize(root, members))

    wm_members: set[int] = set()
    queue = [wm_pid]
    while queue:
        pid = queue.pop()
        if pid in claimed or pid in wm_members:
            continue
        if pid != wm_pid and pid in target_pids:
            continue
        if pid not in ppid_by_pid and pid != wm_pid:
            continue
        wm_members.add(pid)
        queue.extend(
            child
            for child in children.get(pid, [])
            if child not in claimed and child not in target_pids
        )
    wm = _summarize(wm_pid, wm_members)
    return Footprint(wm=wm, runners=runners, untracked=untracked)


def _read_psutil(
    wm_pid: int, targets: list[FootprintTarget], *, platform: str
) -> Footprint:
    ppid_by_pid: dict[int, int] = {}
    try:
        processes = list(psutil.process_iter(["pid", "ppid"]))
    except Exception:  # noqa: BLE001
        processes = []
    for proc in processes:
        try:
            info = proc.info
            ppid_by_pid[int(info["pid"])] = int(info["ppid"])
        except (KeyError, TypeError, ValueError):
            continue

    children: dict[int, list[int]] = {}
    for pid, ppid in ppid_by_pid.items():
        children.setdefault(ppid, []).append(pid)

    has_getpgid = hasattr(os, "getpgid") and not platform.startswith("win")
    pgid_by_pid: dict[int, int] = {}
    if has_getpgid:
        for pid in ppid_by_pid:
            try:
                pgid_by_pid[pid] = os.getpgid(pid)
            except OSError:
                continue

    def _close(seed: set[int], claimed: set[int]) -> set[int]:
        members: set[int] = set()
        queue = [pid for pid in seed if pid not in claimed]
        target_pids = {target.pid for target in targets}
        while queue:
            pid = queue.pop()
            if pid in claimed or pid in members:
                continue
            if pid not in ppid_by_pid and pid != wm_pid and pid not in target_pids:
                continue
            members.add(pid)
            queue.extend(
                child
                for child in children.get(pid, [])
                if child not in claimed and child not in members
            )
        return members

    def _summarize(root: int, members: set[int]) -> TreeFootprint:
        rss = 0
        count = 0
        for pid in members:
            try:
                rss += int(psutil.Process(pid).memory_info().rss) // 1024
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
            count += 1
        return TreeFootprint(pid=root, process_count=count, rss_kb=rss, swap_kb=None)

    claimed: set[int] = set()
    runners: dict[str, TreeFootprint] = {}
    for target in targets:
        seed = {target.pid}
        if has_getpgid:
            for pid, pgid in pgid_by_pid.items():
                if pgid == target.pid:
                    seed.add(pid)
        members = _close(seed, claimed)
        claimed.update(members)
        runners[target.runner_id] = _summarize(target.pid, members)

    wm_members: set[int] = set()
    queue = [wm_pid]
    while queue:
        pid = queue.pop()
        if pid in claimed or pid in wm_members:
            continue
        wm_members.add(pid)
        queue.extend(child for child in children.get(pid, []) if child not in claimed)
    wm = _summarize(wm_pid, wm_members)
    return Footprint(wm=wm, runners=runners, untracked=[])
