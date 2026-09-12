"""Checking and removing an ER env must not destroy healthy envs or stall the WM.

prepare-envs checks every project's env at once and answers "invalid" by
deleting and recreating it. On a loaded machine a healthy env's version check
can miss a short deadline, so a timeout is retried before the env is condemned,
and every invalid verdict carries its reason so the log explains a recreation
without debug logging. Deleting a venv touches thousands of files, so it must
not run on the WM's event loop.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from loguru import logger

from finecode.wm_server import context
from finecode.wm_server.runner import finecode_cmd, runner_manager
from finecode.wm_server.services import process_budget


def _fake_python(tmp_path: Path, body: str) -> Path:
    # Slow bodies `exec` their sleep: a killed shell would leave the sleep
    # holding the stdout pipe, and asyncio's wait() waits for every pipe.
    script = tmp_path / "fake_python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_manager, "VERSION_CHECK_TIMEOUTS_SEC", (0.3, 5.0))


def _use_python(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    monkeypatch.setattr(
        finecode_cmd, "get_python_cmd", lambda _dir, _env: script.as_posix()
    )


async def test_slow_version_check_is_retried_before_the_env_is_condemned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_timeouts: None
) -> None:
    """A check that misses the first deadline but answers on retry is valid.

    Treating the first timeout as a verdict deletes a healthy env whenever the
    machine is busy, which prepare-envs' own fan-out makes it.
    """
    marker = tmp_path / "called_once"
    script = _fake_python(
        tmp_path,
        f'if [ -e "{marker}" ]; then echo "FineCode Extension Runner 1.0";'
        f' else touch "{marker}"; exec sleep 10; fi',
    )
    _use_python(monkeypatch, script)
    warnings: list[str] = []
    sink_id = logger.add(
        lambda m: warnings.append(m.record["message"]), level="WARNING"
    )
    try:
        check = await runner_manager.check_runner(tmp_path, "dev_workspace")
    finally:
        logger.remove(sink_id)

    assert check.valid
    assert any("did not finish within 0.3s" in w for w in warnings)


async def test_version_check_that_never_finishes_reports_the_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_manager, "VERSION_CHECK_TIMEOUTS_SEC", (0.2, 0.3))
    _use_python(monkeypatch, _fake_python(tmp_path, "exec sleep 10"))

    check = await runner_manager.check_runner(tmp_path, "dev_workspace")

    assert not check.valid
    assert "0.2s, 0.3s" in check.reason


async def test_failing_version_check_reports_exit_code_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_timeouts: None
) -> None:
    _use_python(
        monkeypatch,
        _fake_python(
            tmp_path, 'echo "No module named finecode_extension_runner" >&2; exit 1'
        ),
    )

    check = await runner_manager.check_runner(tmp_path, "dev_workspace")

    assert not check.valid
    assert "exited with code 1" in check.reason
    assert "No module named finecode_extension_runner" in check.reason


async def test_missing_venv_reports_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_dir: Path, env_name: str) -> str:
        raise ValueError(f"Execution environment '{env_name}' not found")

    monkeypatch.setattr(finecode_cmd, "get_python_cmd", missing)

    check = await runner_manager.check_runner(tmp_path, "dev_workspace")

    assert not check.valid
    assert "not found" in check.reason


async def test_remove_runner_env_deletes_the_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / ".venvs" / "dev_workspace"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    monkeypatch.setattr(
        finecode_cmd, "get_venv_dir_path", lambda project_path, env_name: venv
    )

    await runner_manager.remove_runner_env(tmp_path, "dev_workspace")

    assert not venv.exists()


async def test_concurrent_env_checks_never_exceed_the_process_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prepare-envs checks every env at once; the checks must share the budget.

    Unbounded, 70+ interpreters start together on the machine the WM shares,
    and the overload makes healthy envs miss their version-check deadline.
    """
    ws_context = context.WorkspaceContext([])
    ws_context.process_budget = process_budget.ProcessBudget(2)
    running = 0
    peak = 0

    async def fake_check(runner_dir: Path, env_name: str):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return runner_manager.RunnerEnvCheck(valid=True)

    monkeypatch.setattr(runner_manager, "check_runner", fake_check)

    results = await asyncio.gather(
        *(
            runner_manager.check_runner_within_budget(
                ws_context, tmp_path / f"p{i}", "dev_workspace"
            )
            for i in range(8)
        )
    )

    assert all(r.valid for r in results)
    assert peak == 2
    assert ws_context.process_budget.granted == 0
