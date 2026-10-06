"""Tests for how the shared pyrefly server receives settings changed after start.

Unlike ruff, pyrefly rereads client settings when the client sends
``workspace/didChangeConfiguration``. A handler built after hover already started the
server can therefore still have its settings applied — but only if the wrapper
notices the change and pushes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest import mock

from finecode_extension_runner.testing import NoOpLogger, nonexistent_abs_path

from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


class _NoopWorkSlots:
    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, None]:
        yield


class _FakeExtensionRunnerInfoProvider:
    def get_current_env_name(self) -> str:
        return "test"

    def get_cache_dir_path(self) -> Path:
        return nonexistent_abs_path("fake", "cache")

    def get_current_venv_dir_path(self) -> Path:
        return nonexistent_abs_path("fake", "venvs", "test")

    def get_venv_dir_path_of_env(self, env_name: str) -> Path:
        return nonexistent_abs_path("fake", "venvs", env_name)

    def get_venv_site_packages(self, venv_dir_path: Path) -> list[Path]:
        return [venv_dir_path / "lib" / "site-packages"]

    def get_venv_python_interpreter(self, venv_dir_path: Path) -> Path:
        return venv_dir_path / "bin" / "python"


class _RecordingLspService:
    """Records settings pushes, and can hold the first start open."""

    def __init__(self) -> None:
        self.ensure_started_calls = 0
        self.send_settings_calls = 0
        self.update_settings_calls: list[dict[str, typing.Any]] = []
        self.ensure_started_gate: asyncio.Event | None = None

    def update_settings(self, settings: dict[str, typing.Any]) -> None:
        self.update_settings_calls.append(settings)

    async def ensure_started(self, _root_uri: str) -> None:
        self.ensure_started_calls += 1
        if self.ensure_started_gate is not None:
            await self.ensure_started_gate.wait()

    async def send_settings(self) -> None:
        self.send_settings_calls += 1


class _FakePyreflyConfig:
    def __init__(self, lsp_config_path: Path | None) -> None:
        self.lsp_config_path = lsp_config_path


def _service(
    lsp_config_path: Path | None = None,
) -> tuple[PyreflyLspService, _RecordingLspService]:
    recorder = _RecordingLspService()
    # Patch the inner LspService so the recorder also sees the settings the
    # constructor pushes, not only the ones pushed after the swap.
    with mock.patch(
        "fine_python_pyrefly.pyrefly_lsp_service.LspService", return_value=recorder
    ):
        service = PyreflyLspService(
            lsp_client=typing.cast(typing.Any, object()),
            file_editor=typing.cast(typing.Any, object()),
            logger=NoOpLogger(),
            extension_runner_info_provider=_FakeExtensionRunnerInfoProvider(),
            work_slots=_NoopWorkSlots(),
            pyrefly_config=typing.cast(typing.Any, _FakePyreflyConfig(lsp_config_path)),
        )
    return service, recorder


async def test_settings_registered_before_the_first_start_are_not_pushed() -> None:
    """A first start already carries the settings and answers configuration pulls
    from the live dict, so pushing them again would be a redundant notification
    on every session's first use."""
    service, recorder = _service()

    service.update_settings({"pyrefly": {"displayTypeErrors": "force-on"}})
    await service.ensure_started("file:///project")

    assert recorder.ensure_started_calls == 1
    assert recorder.send_settings_calls == 0


async def test_a_settings_change_after_start_is_pushed_exactly_once() -> None:
    """A handler that applies its configuration after hover already started the
    server must still have it take effect; losing it silently is why settings
    changed after start are pushed rather than assumed to be unreachable."""
    service, recorder = _service()

    await service.ensure_started("file:///project")
    service.update_settings({"pyrefly": {"configPath": "/cache/pyrefly.toml"}})
    await service.ensure_started("file:///project")
    await service.ensure_started("file:///project")

    assert recorder.ensure_started_calls == 3
    assert recorder.send_settings_calls == 1


async def test_an_update_during_a_start_is_pushed_by_the_next_call() -> None:
    """The start in flight read its settings before the update landed, so it did
    not carry them; treating that update as sent would leave the new handler's
    configuration permanently unapplied for the session."""
    service, recorder = _service()
    recorder.ensure_started_gate = asyncio.Event()

    start = asyncio.create_task(service.ensure_started("file:///project"))
    while recorder.ensure_started_calls == 0:
        await asyncio.sleep(0)
    service.update_settings({"pyrefly": {"configPath": "/cache/pyrefly.toml"}})
    recorder.ensure_started_gate.set()
    await start

    assert recorder.send_settings_calls == 0

    await service.ensure_started("file:///project")

    assert recorder.send_settings_calls == 1


async def test_generated_config_is_in_the_settings_before_the_first_start() -> None:
    """Configured errors must reach the server with its initial settings: a
    configPath applied after the first start leaves diagnostics from that
    start unconfigured until the late push lands."""
    config_path = Path("/cache/pyrefly/pyrefly.toml")
    _, recorder = _service(lsp_config_path=config_path)

    info_provider = _FakeExtensionRunnerInfoProvider()
    expected_extra_paths = [
        str(site_packages)
        for site_packages in info_provider.get_venv_site_packages(
            info_provider.get_venv_dir_path_of_env("runtime")
        )
    ]

    assert recorder.update_settings_calls[-1]["pyrefly"] == {
        "configPath": str(config_path),
        "extraPaths": expected_extra_paths,
    }


async def test_no_config_path_without_configured_errors() -> None:
    """Without configured errors the service must not take over pyrefly's
    config discovery: a generated configPath would shadow a project
    pyrefly.toml the user relies on."""
    _, recorder = _service(lsp_config_path=None)

    assert all(
        "configPath" not in settings.get("pyrefly", {})
        for settings in recorder.update_settings_calls
    )
