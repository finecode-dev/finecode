"""`finecodeRunner/updateProcessBudget` resizes the gate without touching the
runner's handler instances.

A budget change is a process-level tweak. If it rebuilt RunnerContext the way
`updateConfig` does, every handler would be torn down and re-instantiated just
because the WM rebalanced its process budget — losing in-flight state for no
reason.
"""

from __future__ import annotations

import pytest

from finecode_extension_runner import er_server, process_slots
from finecode_extension_runner.process_slots import ProcessSlots


class _FakeServer:
    def __init__(self) -> None:
        self._runner_context = object()


async def test_update_process_budget_does_not_rebuild_runner_context() -> None:
    process_slots.set_process_slots(ProcessSlots(target=1))
    try:
        server = _FakeServer()
        before = server._runner_context

        await er_server.update_process_budget(server, {"target": 4})

        assert server._runner_context is before
        assert process_slots.get_process_slots().target == 4
    finally:
        process_slots.reset_process_slots()


async def test_update_process_budget_rejects_non_integer_target() -> None:
    process_slots.set_process_slots(ProcessSlots(target=1))
    try:
        server = _FakeServer()

        with pytest.raises(ValueError, match="integer"):
            await er_server.update_process_budget(server, {"target": "four"})
    finally:
        process_slots.reset_process_slots()
