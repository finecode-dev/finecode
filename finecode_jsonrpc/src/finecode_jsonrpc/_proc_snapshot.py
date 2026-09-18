from __future__ import annotations

import os
import sys
import typing

__all__ = ["describe_process_group"]


class _ProcStat(typing.NamedTuple):
    comm: str
    state: str
    pgrp: int
    utime: int
    stime: int


def _parse_stat(text: str) -> _ProcStat:
    """Parse ``/proc/<pid>/stat``.

    ``comm`` is wrapped in parentheses and may itself contain spaces or
    parentheses, so the fields after it are read from the *last* ``)`` — a
    naive ``split()`` would misalign on such a name.
    """
    comm = text[text.index("(") + 1 : text.rfind(")")]
    fields = text[text.rfind(")") + 2 :].split()
    return _ProcStat(
        comm=comm,
        state=fields[0],
        pgrp=int(fields[2]),
        utime=int(fields[11]),
        stime=int(fields[12]),
    )


def _read_status_memory(pid: int) -> tuple[int, int]:
    """(VmRSS, VmSwap) in kB; zeros where the kernel does not report them."""
    rss_kb = 0
    swap_kb = 0
    with open(f"/proc/{pid}/status") as status_file:
        for line in status_file:
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("VmSwap:"):
                swap_kb = int(line.split()[1])
    return rss_kb, swap_kb


def describe_process_group(pgid: int, limit: int = 10) -> str:
    """One diagnostic line per process in process group *pgid*, or ``""`` off Linux.

    A start failure needs to say what the spawned process group was doing at the
    deadline: a process group with no members means the shell already exited, one
    running at full CPU is working, one parked in ``D`` state is blocked on I/O.
    Never raises: a snapshot failure must not replace the start error it is
    explaining.
    """
    if not sys.platform.startswith("linux"):
        return ""

    try:
        clock_ticks = os.sysconf("SC_CLK_TCK")
        lines: list[str] = []
        for entry in sorted(
            os.listdir("/proc"), key=lambda name: int(name) if name.isdigit() else 0
        ):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat") as stat_file:
                    stat = _parse_stat(stat_file.read())
                if stat.pgrp != pgid:
                    continue
                rss_kb, swap_kb = _read_status_memory(pid)
            except OSError:
                # The process vanished while we were reading it — it is not
                # part of the snapshot, and not an error.
                continue
            cpu_sec = (stat.utime + stat.stime) / clock_ticks
            lines.append(
                f"{pid} {stat.comm} state={stat.state}"
                f" cpu={cpu_sec:.2f}s rss={rss_kb // 1024}MB swap={swap_kb // 1024}MB"
            )
            if len(lines) >= limit:
                break
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return "process group snapshot unavailable"
