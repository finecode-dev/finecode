"""Structural guards for the CI workflow's private-layer job.

The private layer is gated through GitHub's expression contexts, and the wrong
context fails *silently*: `secrets` is not available in a job-level `if:`, so an
expression that reads it evaluates to empty and skips the job forever without
any error. These tests assert the properties that keep the gate on the contexts
that actually carry the values — the `github` context at job level, and a
secrets-derived `env` value at step level — so a broken gate is caught here
rather than discovered as a permanently green-but-skipped job.

PyYAML is not a dependency of the testing environments, so the workflow is
inspected line-wise rather than parsed as YAML. The assertions target only the
structural facts they guard; they do not validate the file end-to-end.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"

_STEP_START_RE = re.compile(r"^      - (name|uses):\s*(.*)$")
_STEP_IF_RE = re.compile(r"^        if:\s*(.*)$")


def _workflow_text() -> str:
    return _WORKFLOW_PATH.read_text(encoding="utf-8")


def _job_block(text: str, job_name: str) -> list[str]:
    """The body lines of `job_name`, up to the next sibling job key."""
    lines = text.splitlines()
    start = next(i + 1 for i, line in enumerate(lines) if line == f"  {job_name}:")
    block: list[str] = []
    for line in lines[start:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        block.append(line)
    assert block, f"job {job_name!r} has an empty body"
    return block


def _steps(block: list[str]) -> list[dict[str, str]]:
    """`[{key: name|uses, value, if}]` for each step in a job body."""
    steps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in block:
        start = _STEP_START_RE.match(line)
        if start:
            current = {"key": start.group(1), "value": start.group(2), "if": ""}
            steps.append(current)
            continue
        if current is not None:
            if_match = _STEP_IF_RE.match(line)
            if if_match:
                current["if"] = if_match.group(1)
    return steps


def test_no_if_expression_references_secrets() -> None:
    """No step or job `if:` reads `secrets.` — that context is unavailable there, so the test would silently skip forever."""
    violations = [
        (i, line)
        for i, line in enumerate(_workflow_text().splitlines(), start=1)
        if re.match(r"^\s*if:\s*", line) and "secrets." in line
    ]
    assert not violations, f"`if:` reads `secrets.` (unavailable context): {violations}"


def test_audit_private_job_platform_and_timeout() -> None:
    """The private audit runs on a Linux runner with a budget that fits a cold prepare-envs plus a workspace-wide audit."""
    block = _job_block(_workflow_text(), "audit-private")
    joined = "\n".join(block)
    assert re.search(r"^    runs-on:\s*ubuntu-24.04\s*$", joined, re.MULTILINE)
    timeout = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", joined, re.MULTILINE)
    assert timeout is not None
    assert int(timeout.group(1)) >= 60


def test_audit_private_step_order() -> None:
    """The private job clones both private repos and copies the user file before the cache and checks that depend on them."""
    block = _job_block(_workflow_text(), "audit-private")
    steps = _steps(block)

    assert steps[0]["key"] == "uses"
    assert steps[0]["value"] == "actions/checkout@v5"

    ordered = [
        "Mint private-clone token",
        "Check out fine_knowledge",
        "Check out internal experiments",
        "Install the CI private-layer config",
        "Cache all venvs",
        "Install dependencies",
        "Inspect code",
        "Extract knowledge",
        "Audit code",
        "Run unit tests",
    ]
    names = [step["value"] for step in steps if step["key"] == "name"]
    positions = [names.index(name) for name in ordered]
    assert positions == sorted(positions), f"steps out of order: {ordered}"


def test_audit_private_cache_key_namespace_and_hash_inputs() -> None:
    """The private venv cache is namespaced apart from the public job's and keys on the user file copied in before it."""
    block = _job_block(_workflow_text(), "audit-private")
    key = next(line.strip() for line in block if re.match(r"^\s*key:\s*", line))
    assert key.startswith("key: ${{ runner.os }}-private-venvs-"), key
    assert "finecode-user.toml" in key


def test_audit_private_steps_gated_on_has_private_clone_app() -> None:
    """Steps after the public checkout are gated on the credentials bridge; the one exception is the skip notice, which runs only when credentials are absent."""
    block = _job_block(_workflow_text(), "audit-private")
    steps = _steps(block)

    assert steps[0]["if"] == ""
    for step in steps[1:]:
        if step["value"] == "Report skip reason":
            assert step["if"] == "env.HAS_PRIVATE_CLONE_APP != 'true'", step
        else:
            assert step["if"] == "env.HAS_PRIVATE_CLONE_APP == 'true'", step
