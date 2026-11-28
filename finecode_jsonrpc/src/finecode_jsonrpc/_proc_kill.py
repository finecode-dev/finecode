"""Force-kill a spawned server and every process it started.

Servers are spawned with ``start_new_session=True`` on POSIX, so the recorded
pid leads its own process group and one group kill reaps every child the server
started. Windows has no process-group equivalent; ``taskkill /T`` walks the
process tree instead.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from loguru import logger

__all__ = ["force_kill_process_group"]


def force_kill_process_group(
    pid: int, readable_id: str, *, descendant_groups: bool = False
) -> None:
    """SIGKILL the process group *pid* leads, or the process tree on Windows.

    With *descendant_groups* (POSIX only), also SIGKILL each distinct group
    of the snapshot taken before the leader's own group kill: owned commands
    and `start_long_running` children run in sessions of their own, so the
    leader's group kill alone leaves them behind. A no-op when no member of
    a group can be signalled any more: the process already exited, or (macOS)
    only zombies are left.
    """
    if sys.platform == "win32":
        # os.killpg has no Windows equivalent; taskkill /T walks the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    descendant_pgids = _snapshot_descendant_groups(pid) if descendant_groups else []
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS reports EPERM for a group whose only members are zombies;
        # either way nothing left in it is ours to signal.
        logger.debug(
            f"Process group {pid} of {readable_id} could not be signalled (EPERM)"
        )
    own_pg = os.getpgrp()
    for pgid in descendant_pgids:
        if pgid == pid or pgid <= 1 or pgid == own_pg:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _snapshot_descendant_groups(pid: int) -> list[int]:
    """Distinct pgids of *pid*'s living descendants, before any kill."""
    try:
        import psutil
    except ImportError:
        return []
    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []
    pgids: list[int] = []
    for child in children:
        try:
            pgid = os.getpgid(child.pid)
        except OSError:
            continue
        if pgid not in pgids:
            pgids.append(pgid)
    return pgids
