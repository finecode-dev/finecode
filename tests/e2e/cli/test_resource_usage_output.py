"""The reporter must observe a real run without changing its outcome."""

from __future__ import annotations

import os
import subprocess
import sys


def _run(
    args: list[str], workspace_dir, env: dict | None = None
) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "finecode", *args],
        cwd=workspace_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        env=merged,
    )


def test_resource_usage_interval_prints_summary_on_dedicated_server(
    workspace_dir_with_er,
):
    """A real run with an interval must print the peaks summary on stderr only."""
    flagged = _run(
        ["run", "--resource-usage=0.2", "test_action"], workspace_dir_with_er
    )
    plain = _run(["run", "--no-resource-usage", "test_action"], workspace_dir_with_er)

    assert "[resources] peaks:" in flagged.stderr
    assert "[resources]" not in flagged.stdout
    assert flagged.returncode == plain.returncode


def test_no_resource_usage_disables_ci_default(workspace_dir_with_er):
    """The off switch must silence the CI default-on reporter."""
    result = _run(
        ["run", "--no-resource-usage", "test_action"],
        workspace_dir_with_er,
        env={"CI": "true"},
    )

    assert "[resources]" not in result.stderr
