from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from fine_envs import create_env_action
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_runner.impls.command_runner import (
    AsyncProcess,
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.owned_commands import OwnedCommandRegistry
from finecode_extension_runner.process_slots import ProcessSlots
from finecode_extension_runner.testing import NoOpLogger, run_handler

from fine_python_uv import create_env_handler

from .test_create_env_handler import _env_info, _service_overrides


class _ExposingScope:
    """Rewrites argv through the public scope while exposing the process."""

    def __init__(
        self,
        inner: contextlib.AbstractAsyncContextManager[AsyncProcess],
        runner: _UvStandInRunner,
    ) -> None:
        self._inner = inner
        self._runner = runner

    async def __aenter__(self) -> AsyncProcess:
        process = await self._inner.__aenter__()
        self._runner.process = process
        self._runner.spawned.set()
        return process

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return await self._inner.__aexit__(exc_type, exc, tb)


class _UvStandInRunner(CommandRunner):
    def __init__(self, script: pathlib.Path) -> None:
        self.slots = ProcessSlots(target=1)
        self.leases: set[str] = set()
        self.released: list[str] = []
        self.slots.attach_budget(self._lease, self._release)
        super().__init__(
            logger=NoOpLogger(),
            config=CommandRunnerConfig(max_concurrent_processes=1),
            process_slots=self.slots,
            owned_commands=OwnedCommandRegistry(),
        )
        self.script = script
        self.process: AsyncProcess | None = None
        self.cwd: pathlib.Path | None = None
        self.spawned = asyncio.Event()

    async def _lease(self) -> str:
        self.leases.add("uv-create")
        return "uv-create"

    async def _release(self, lease_id: str) -> None:
        self.leases.remove(lease_id)
        self.released.append(lease_id)

    def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
    ) -> contextlib.AbstractAsyncContextManager[AsyncProcess]:
        assert list(cmd)[1:3] == ["venv", "--clear"]
        self.cwd = cwd
        inner = super().run(
            [sys.executable, str(self.script), *list(cmd)[1:]],
            cwd=cwd,
            env=env,
            new_process_group=new_process_group,
        )
        return _ExposingScope(inner, self)

    def is_finished(self) -> bool:
        assert self.process is not None
        return (
            not self.process.is_alive()
            and self.process.get_exit_code() is not None
            and self.process._drain.done()
            and self._owned_commands.open_count == 0
            and self.slots.in_flight == 0
            and not self.leases
            and self.released == ["uv-create"]
            and self._local_cap is not None
            and not self._local_cap.locked()
        )


async def _run_create(tmp_path: pathlib.Path, runner: _UvStandInRunner) -> Any:
    defs = tmp_path / "defs"
    defs.mkdir(exist_ok=True)
    dump_file = defs / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    from finecode_extension_api.resource_uri import path_to_resource_uri as _to_uri

    return await run_handler(
        create_env_handler.UvCreateEnvHandler,
        create_env_action.CreateEnvRunPayload(
            env=_env_info(tmp_path),
            recreate=False,
            resolved_project_def_path=_to_uri(dump_file),
        ),
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(cast(Any, runner)),
    )


async def test_cancelled_create_reaps_uv_before_removing_its_input(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "uv_stand_in.py"
    _ = script.write_text(
        "import os, signal, time\n"
        "if os.name == 'posix':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    runner = _UvStandInRunner(script)
    task = asyncio.create_task(_run_create(tmp_path, runner))
    try:
        await asyncio.wait_for(runner.spawned.wait(), timeout=10)
        assert runner.process is not None
        lines = cast(AsyncGenerator[str, None], runner.process.stdout_lines())
        assert await asyncio.wait_for(anext(lines), timeout=10) == "ready"
        await lines.aclose()
        assert runner.process.is_alive()
        assert runner.slots.in_flight == 1
        assert runner.leases == {"uv-create"}
        assert runner.cwd is not None and runner.cwd.exists()

        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=10)

        assert not runner.process.is_alive(), "cancelled creation left uv running"
        assert runner.is_finished(), "process drain or work slot outlived creation"
        assert runner.cwd is not None
        assert (runner.cwd / "pyproject.toml").is_file()
        assert runner.cwd.exists()
    finally:
        if not task.done():
            _ = task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if runner.process is not None and runner.process.is_alive():
            runner.process.kill()
            await runner.process.wait_for_end(timeout=10)


@pytest.mark.parametrize(
    ("exit_code", "output_stream"), [(0, "stdout"), (7, "stderr"), (9, "stdout")]
)
async def test_create_preserves_real_process_results(
    tmp_path: pathlib.Path, exit_code: int, output_stream: str
) -> None:
    script = tmp_path / "uv_stand_in.py"
    _ = script.write_text(
        "import sys\n"
        f"print('uv result detail', file=sys.{output_stream})\n"
        f"sys.exit({exit_code})\n"
    )
    runner = _UvStandInRunner(script)

    result = await _run_create(tmp_path, runner)

    assert result is not None
    assert result.created is True
    if exit_code == 0:
        assert result.errors == []
    else:
        assert len(result.errors) == 1
        assert "Failed to create virtualenv" in result.errors[0]
        assert "uv result detail" in result.errors[0]
        assert "supplied project definition file" in result.errors[0]
        assert "dump-config" not in result.errors[0]
    assert runner.is_finished()
    assert runner.cwd is not None and runner.cwd.exists()
    assert (runner.cwd / "pyproject.toml").is_file()
