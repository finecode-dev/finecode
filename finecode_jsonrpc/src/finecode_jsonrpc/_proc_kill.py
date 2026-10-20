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


def force_kill_process_group(pid: int, readable_id: str) -> None:
    """SIGKILL the process group *pid* leads, or the process tree on Windows.

    A no-op when no member of the group can be signalled any more: the process
    already exited, or (macOS) only zombies are left.
    """
    if sys.platform == "win32":
        # os.killpg has no Windows equivalent; taskkill /T walks the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS reports EPERM for a group whose only members are zombies;
            # either way nothing left in it is ours to signal.
            logger.debug(
                f"Process group {pid} of {readable_id} could not be signalled"
                " (EPERM)"
            )
