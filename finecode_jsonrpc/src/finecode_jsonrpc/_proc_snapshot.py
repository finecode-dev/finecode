"""Per-platform diagnostic snapshot of a spawned server's processes.

A start failure needs to say what the spawned server was doing at the
deadline: its processes listed, "no live process" if it already exited, or
"snapshot unavailable" with a reason. Never raises: a snapshot failure must
not replace the start error it is explaining.
"""

from __future__ import annotations

import os
import sys
import typing

import psutil

__all__ = ["describe_spawned_processes"]

# ``os.getpgid`` is POSIX-only in typeshed, but the name must resolve on
# Windows too, where CI-win's type check runs with the win32 platform. Probed
# 2026-09-24: a direct ``os.getpgid`` → ``missing-attribute`` under
# ``--python-platform win32``, so it is looked up dynamically at module level.
_getpgid = getattr(os, "getpgid", None)


def describe_spawned_processes(
    root_pid: int, limit: int = 10, platform: str = sys.platform
) -> str:
    """One diagnostic line per process the spawned server consists of.

    POSIX: the server is spawned with ``start_new_session=True``, so its pid is
    its process group; every member is listed, including children reparented
    after their parent exited. Windows has no process group: the tree rooted at
    *root_pid*, by parent pid (see issue 43 for the Job Object replacement).
    Returns the lines, ``"no live process in …"``, or
    ``"process snapshot unavailable: <reason>"``. Never raises: a snapshot
    failure must not replace the start error it is explaining.
    """
    by_group = platform != "win32"
    with_swap = platform.startswith("linux")
    with_state = platform != "win32"
    with_tree_fields = platform == "win32"

    try:
        members = _members(root_pid, by_group)
        if not members:
            if by_group:
                return f"no live process in process group {root_pid}"
            return f"no live process in the process tree of {root_pid}"

        lines: list[str] = []
        for pid in sorted(members):
            process = members[pid]
            with process.oneshot():
                try:
                    name = process.name()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    # It vanished mid-read — it is not part of the snapshot,
                    # and not an error.
                    continue
                fields = [f"{pid} {name}"]
                if with_state:
                    fields.append(f"state={_read_status(process)}")
                if with_tree_fields:
                    fields.append(f"ppid={_read_ppid(process)}")
                fields.append(f"cpu={_read_cpu_seconds(process)}")
                fields.append(f"rss={_read_rss(process)}")
                if with_swap:
                    fields.append(f"swap={_read_swap(process)}")
                if with_tree_fields:
                    fields.append(f"threads={_read_threads(process)}")
                lines.append(" ".join(fields))
            if len(lines) >= limit:
                break
        return "\n".join(lines)
    except Exception as exception:  # noqa: BLE001
        return f"process snapshot unavailable: {type(exception).__name__}: {exception}"


def _members(root_pid: int, by_group: bool) -> dict[int, psutil.Process]:
    """The processes the spawned server consists of, keyed by pid.

    Tree (Windows): ``Process(root_pid)`` plus ``.children(recursive=True)``
    — psutil drops a child whose creation time is earlier than the parent's,
    which is the pid-reuse guard. If the root is dead, every process whose
    parent pid equals *root_pid*, plus their descendants, is added instead:
    Windows never rewrites ppid, so a child orphaned by its server's exit is
    still found this way.

    Group (POSIX): every process whose process group id equals *root_pid*,
    found through ``os.getpgid`` even when a member was reparented after its
    parent exited.
    """
    members: dict[int, psutil.Process] = {}
    if not by_group:
        try:
            members[root_pid] = psutil.Process(root_pid)
        except psutil.NoSuchProcess:
            for entry in psutil.process_iter(["ppid"]):
                if entry.info["ppid"] == root_pid:
                    members[entry.pid] = entry
        for pid in list(members):
            _add_children(members, pid)
    elif _getpgid is not None:
        for entry in psutil.process_iter():
            try:
                if _getpgid(entry.pid) == root_pid:
                    members[entry.pid] = entry
            except (ProcessLookupError, PermissionError):
                # Vanished or owned by another user — not part of the group.
                continue
    return members


def _add_children(members: dict[int, psutil.Process], parent_pid: int) -> None:
    try:
        children = members[parent_pid].children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in children:
        members.setdefault(child.pid, child)


def _read_status(process: psutil.Process) -> str:
    return _read(lambda: str(process.status()))


def _read_ppid(process: psutil.Process) -> str:
    return _read(lambda: str(process.ppid()))


def _read_threads(process: psutil.Process) -> str:
    return _read(lambda: str(process.num_threads()))


def _read_cpu_seconds(process: psutil.Process) -> str:
    return _read(
        lambda: f"{process.cpu_times().user + process.cpu_times().system:.2f}s"
    )


def _read_rss(process: psutil.Process) -> str:
    return _read(lambda: f"{process.memory_info().rss // 2**20}MB")


def _read_swap(process: psutil.Process) -> str:
    return _read(lambda: f"{process.memory_full_info().swap // 2**20}MB")


def _read(read: typing.Callable[[], object]) -> str:
    """One per-process field, stringified; ``?`` when the read is denied.

    ``AccessDenied`` is expected for processes owned by another user; a process
    that vanished mid-read is reported the same way rather than dropped for the
    rest of its line.
    """
    try:
        return str(read())
    except psutil.AccessDenied:
        return "?"
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return "?"
