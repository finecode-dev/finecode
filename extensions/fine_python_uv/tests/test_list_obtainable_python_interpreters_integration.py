"""Integration test: run the handler against the real `uv` binary.

The fixture unit tests pin the handler's filtering logic against an *assumed* uv JSON
shape. This test closes the gap they cannot: if a real uv release renames a field or
restructures its output, the fixture stays green while production breaks. Here the real
provisioner runs, so the assumptions about its output are exercised for real.

Assertions are deliberately loose — the exact version set changes as CPython releases —
and cover only the structural invariants the handler depends on: a non-empty result,
every entry in canonical ``impl@X.Y`` form (no patch level, variant, or platform tag),
no duplicate minors, and CPython present. `uv python list --only-downloads` reads uv's
embedded manifest and needs no network.
"""

from __future__ import annotations

import re

import pytest
from finecode_extension_runner.testing import run_handler

from fine_python_lang.list_obtainable_python_interpreters_action import (
    ListObtainablePythonInterpretersAction,
    ListObtainablePythonInterpretersRunPayload,
)
from fine_python_uv._uv_common import get_uv_executable
from fine_python_uv.list_obtainable_python_interpreters_handler import (
    UvListObtainablePythonInterpretersHandler,
)

pytestmark = pytest.mark.skipif(
    not get_uv_executable().exists(),
    reason="uv binary not available in this environment",
)

_CANONICAL = re.compile(r"^[a-z][a-z0-9]*@\d+\.\d+$")


async def test_real_uv_output_matches_handler_assumptions() -> None:
    result = await run_handler(
        UvListObtainablePythonInterpretersHandler,
        ListObtainablePythonInterpretersRunPayload(),
        action_cls=ListObtainablePythonInterpretersAction,
    )

    assert result is not None
    # a broken parse against real uv would silently yield an empty list, so this is the
    # load-bearing assertion: the handler actually understood uv's output
    assert result.toolchains, "handler parsed no toolchains from real uv output"

    for toolchain in result.toolchains:
        assert _CANONICAL.match(toolchain), f"not reduced to canonical form: {toolchain}"

    assert len(result.toolchains) == len(set(result.toolchains)), (
        "patch levels were not collapsed to one entry per minor"
    )
    assert any(t.startswith("cpython@") for t in result.toolchains)
