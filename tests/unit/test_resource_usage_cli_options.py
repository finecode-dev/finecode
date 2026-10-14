"""The CI default-on reporter must be wired, not just implemented."""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from finecode.cli_app import cli as cli_module
from finecode.cli_app import resource_usage
from finecode.cli_app.commands import prepare_envs_cmd, run_cmd

ENV_VAR = resource_usage.ENV_VAR


@pytest.fixture
def _no_logs(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.logger_utils, "init_logger", lambda *a, **k: None)


def _invoke_run(args: list[str]):
    return CliRunner().invoke(cli_module.run, args)


def test_run_rejects_non_numeric_interval(_no_logs, monkeypatch, tmp_path) -> None:
    """A mistyped interval must fail naming the flag, not with a traceback.

    A float() traceback would leave a ValueError as the result exception.
    """
    monkeypatch.chdir(tmp_path)

    async def _unreachable(*args, **kwargs):
        raise AssertionError("run_actions reached")

    monkeypatch.setattr(run_cmd, "run_actions", _unreachable)
    result = _invoke_run(["--resource-usage=abc", "test_action"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "--resource-usage" in result.output


def test_prepare_envs_rejects_both_switches(_no_logs, monkeypatch, tmp_path) -> None:
    """Both switches together must fail before any env is touched."""
    monkeypatch.chdir(tmp_path)

    async def _unreachable(*args, **kwargs):
        raise AssertionError("prepare_envs reached")

    monkeypatch.setattr(prepare_envs_cmd, "prepare_envs", _unreachable)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.prepare_envs,
        ["--resource-usage", "--no-resource-usage"],
    )

    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("env", "args", "expected"),
    [
        ({"CI": "true"}, [], 15.0),
        ({}, [], None),
        ({"CI": "true", ENV_VAR: "0"}, [], None),
        ({"CI": "true", ENV_VAR: "5"}, [], 5.0),
        ({}, ["--resource-usage=3"], 3.0),
        ({}, ["--resource-usage"], 15.0),
    ],
)
def test_run_default_on_wiring(
    _no_logs, monkeypatch, tmp_path, env, args, expected
) -> None:
    """The run command must pass CI's default interval through to the runner."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(ENV_VAR, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    recorded: dict = {}

    async def _fake_run_actions(*args, **kwargs):
        recorded.update(kwargs)
        raise run_cmd.RunFailed("recorded")

    monkeypatch.setattr(run_cmd, "run_actions", _fake_run_actions)
    result = _invoke_run([*args, "--no-save-results", "test_action"])

    assert result.exit_code == 1
    assert recorded.get("resource_usage_interval") == expected


@pytest.mark.parametrize(
    ("env", "args", "expected"),
    [
        ({"CI": "true"}, [], 15.0),
        ({}, [], None),
        ({"CI": "true", ENV_VAR: "0"}, [], None),
        ({"CI": "true", ENV_VAR: "5"}, [], 5.0),
        ({}, ["--resource-usage=3"], 3.0),
        ({}, ["--resource-usage"], 15.0),
    ],
)
def test_prepare_envs_default_on_wiring(
    _no_logs, monkeypatch, tmp_path, env, args, expected
) -> None:
    """The prepare-envs command must follow the same default-on rule."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(ENV_VAR, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    recorded: dict = {}

    async def _fake_prepare_envs(*args, **kwargs):
        recorded.update(kwargs)
        raise prepare_envs_cmd.PrepareEnvsFailed("recorded")

    monkeypatch.setattr(prepare_envs_cmd, "prepare_envs", _fake_prepare_envs)
    runner = CliRunner()
    result = runner.invoke(cli_module.prepare_envs, args)

    assert result.exit_code == 1
    assert recorded.get("resource_usage_interval") == expected


def _minimal_snapshot() -> dict:
    return {
        "timestamp": 100.0,
        "wm": {
            "pid": 1,
            "uptimeSec": 10.0,
            "connectedClients": 0,
            "loopLagMs": None,
            "loopLagMaxMs": None,
            "loopLagPendingMs": None,
            "loopLagWindowSec": 30.0,
        },
        "projects": {"total": 0, "running": 0, "active": 0},
        "runners": {
            "byStatus": {},
            "running": 0,
            "starting": 0,
            "active": 0,
            "byEnv": {},
        },
        "budget": {"total": 7, "source": "test"},
        "workSlots": {
            "total": 4,
            "used": 0,
            "free": 4,
            "waiting": 0,
            "stallEscape": False,
            "holders": [],
        },
        "startupSlots": {"total": 3, "used": 0, "free": 3, "waiting": 0},
        "inFlightRuns": [],
        "peaks": {
            "runnersRunning": 0,
            "runnersStarting": 0,
            "projectsActive": 0,
            "workSlotsUsed": 0,
            "workSlotsWaiting": 0,
            "startupSlotsWaiting": 0,
            "hostSwapUsedMb": None,
            "hostMemAvailableMinMb": None,
            "hookFailed": False,
        },
        "host": {
            "memTotalMb": None,
            "memAvailableMb": None,
            "swapTotalMb": None,
            "swapUsedMb": None,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": None,
                "ioFullAvg10": None,
                "cpuSomeAvg10": None,
            },
            "load1m": None,
            "cpuCount": None,
        },
        "processes": None,
    }


async def test_prepare_envs_wrap_present(monkeypatch, tmp_path, capsys) -> None:
    """Giving prepare-envs an interval must print the peaks summary."""
    recorded: dict = {}

    class _FakeClient:
        async def connect(self, *args, **kwargs) -> None:
            return None

        def on_notification(self, *args, **kwargs) -> None:
            return None

        async def close(self) -> None:
            return None

        async def get_resource_usage(self, **kwargs):
            recorded.update(kwargs)
            return _minimal_snapshot()

    async def _fake_ensure(*args, **kwargs) -> None:
        return None

    async def _fake_ready(*args, **kwargs) -> int:
        return 1234

    async def _sleep_run(*args, **kwargs) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(prepare_envs_cmd.wm_lifecycle, "ensure_running", _fake_ensure)
    monkeypatch.setattr(prepare_envs_cmd.wm_lifecycle, "wait_until_ready", _fake_ready)
    monkeypatch.setattr(prepare_envs_cmd, "ApiClient", lambda: _FakeClient())
    monkeypatch.setattr(prepare_envs_cmd, "_run", _sleep_run)
    await prepare_envs_cmd.prepare_envs(
        tmp_path, False, own_server=False, resource_usage_interval=0.01
    )

    assert "[resources] peaks:" in capsys.readouterr().err
    assert recorded.get("lag_window_sec") == 30.0


async def test_run_actions_wrap_present(monkeypatch, tmp_path, capsys) -> None:
    """Giving run an interval must print the peaks summary with the window."""
    import finecode.cli_app.commands.run_cmd as run_cmd_module

    recorded: dict = {}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False

        def configure_reconnect(self, *args, **kwargs) -> None:
            pass

        def on_notification(self, *args, **kwargs) -> None:
            pass

        def on_request(self, *args, **kwargs) -> None:
            pass

        async def connect(self, *args, **kwargs) -> None:
            pass

        async def add_dir(self, *args, **kwargs) -> dict:
            return {"projects": []}

        async def list_projects(self) -> list:
            return [{"name": "a", "path": str(tmp_path)}]

        async def list_actions(self, **kwargs):
            from finecode.wm_client import ActionListing

            return ActionListing(actions=[], unresolved_projects=[])

        async def get_payload_schemas(self, *args, **kwargs) -> dict:
            return {}

        async def run_batch(self, *args, **kwargs) -> dict:
            await asyncio.sleep(0.05)
            return {"results": {}, "returnCode": 0}

        async def get_resource_usage(self, **kwargs):
            recorded.update(kwargs)
            return _minimal_snapshot()

        async def close(self) -> None:
            self.closed = True

    async def _fake_wait_ready(*args, **kwargs) -> int:
        return 1234

    monkeypatch.setattr(
        run_cmd_module.wm_lifecycle, "ensure_running", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_cmd_module.wm_lifecycle, "wait_until_ready", _fake_wait_ready
    )
    monkeypatch.setattr(run_cmd_module, "ApiClient", _FakeClient)
    await run_cmd_module.run_actions(
        workdir_path=tmp_path,
        projects_names=None,
        actions=[],
        action_payload={},
        raw_action_payload={},
        concurrently=False,
        own_server=False,
        resource_usage_interval=0.01,
    )

    assert "[resources] peaks:" in capsys.readouterr().err
    assert recorded.get("lag_window_sec") == 30.0
