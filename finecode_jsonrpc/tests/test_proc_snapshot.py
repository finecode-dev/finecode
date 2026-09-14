"""Requirement tests (R4): a stalled process group must be describable.

REQUIREMENT: at a port-handshake deadline, the diagnostic must name what the
spawned process group is actually doing — an empty group (the shell exited), a
group running at full CPU (it is working), or one parked in ``D`` state (it is
blocked). Parsing ``/proc`` must survive a process name with spaces or
parentheses, and must never raise over the original start error.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from finecode_jsonrpc import _proc_snapshot


def test_parse_stat_survives_parentheses_in_the_command_name() -> None:
    """``comm`` is wrapped in parentheses and can itself contain them; splitting
    on whitespace would misalign every field after it."""
    stat = _proc_snapshot._parse_stat(
        "4242 (a) b) S 1 4242 4242 0 -1 0 100 0 0 0 21 12"
    )

    assert stat.comm == "a) b"
    assert stat.state == "S"
    assert stat.pgrp == 4242
    assert stat.utime == 21
    assert stat.stime == 12


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="requires /proc (Linux only)"
)
def test_describe_process_group_lists_a_live_process() -> None:
    process = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        snapshot = _proc_snapshot.describe_process_group(process.pid)
    finally:
        process.kill()
        process.wait()

    assert "sleep" in snapshot
    assert "state=" in snapshot
