import pathlib
from typing import Any

import pytest
from finecode_extension_api import code_action

from fine_envs import remove_envs_action
from fine_envs.remove_envs_discover_envs_handler import RemoveEnvsDiscoverEnvsHandler


class _FakeProjectInfoProvider:
    def __init__(
        self, project_def_path: pathlib.Path, raw_config: dict[str, Any]
    ) -> None:
        self._project_def_path = project_def_path
        self._raw_config = raw_config

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._project_def_path

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        return self._raw_config


class _FakeExtensionRunnerInfoProvider:
    def __init__(self, project_dir: pathlib.Path) -> None:
        self._project_dir = project_dir

    def get_current_env_name(self) -> str:
        return "dev_workspace"

    def get_current_venv_dir_path(self) -> pathlib.Path:
        return self._project_dir / ".venvs" / "dev_workspace"


class _FakeUserMessenger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...


class _FakeLogger:
    def debug(self, message: str) -> None: ...


def _make_venv(venvs_dir: pathlib.Path, name: str) -> None:
    venv_dir = venvs_dir / name
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")


def _make_handler(
    tmp_path: pathlib.Path,
    declared: list[str],
    user_messenger: _FakeUserMessenger | None = None,
) -> RemoveEnvsDiscoverEnvsHandler:
    raw_config = {"dependency-groups": {name: [] for name in declared}}
    return RemoveEnvsDiscoverEnvsHandler(
        project_info_provider=_FakeProjectInfoProvider(
            tmp_path / "pyproject.toml", raw_config
        ),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        user_messenger=user_messenger or _FakeUserMessenger(),
        logger=_FakeLogger(),
    )


def _make_run_context(
    payload: remove_envs_action.RemoveEnvsRunPayload,
    trigger: code_action.RunActionTrigger = code_action.RunActionTrigger.USER,
) -> remove_envs_action.RemoveEnvsRunContext:
    return remove_envs_action.RemoveEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(trigger=trigger, dev_env=code_action.DevEnv.CLI),
        info_provider=None,  # type: ignore[arg-type]
    )


async def test_discovers_only_orphans(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    for name in ("dev_workspace", "testing", "testing@cpython-3.11"):
        _make_venv(venvs_dir, name)

    handler = _make_handler(
        tmp_path, declared=["dev_workspace", "testing@cpython-3.11"]
    )
    payload = remove_envs_action.RemoveEnvsRunPayload()
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs is not None
    assert [env.name for env in run_context.envs] == ["testing"]


async def test_discovery_never_includes_the_current_env(tmp_path: pathlib.Path) -> None:
    """The current env must never be auto-removed, even if it becomes
    orphaned (e.g. renamed away in config without an ER restart) — same rule
    `_check_guards` enforces for an explicitly-named target, applied silently
    here since discovery never asked for this env by name."""
    venvs_dir = tmp_path / ".venvs"
    for name in ("dev_workspace", "testing"):
        _make_venv(venvs_dir, name)

    # "dev_workspace" (the env `_FakeExtensionRunnerInfoProvider` reports as
    # current) is declared nowhere — the scenario where a rename/preset change
    # orphans the very env the calling Extension Runner is running in.
    handler = _make_handler(tmp_path, declared=[])
    payload = remove_envs_action.RemoveEnvsRunPayload()
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs is not None
    assert [env.name for env in run_context.envs] == ["testing"]


async def test_empty_env_names_is_explicit_no_op(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")
    _make_venv(venvs_dir, "testing")

    handler = _make_handler(tmp_path, declared=["dev_workspace"])
    payload = remove_envs_action.RemoveEnvsRunPayload(env_names=[])
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs == []


async def test_declared_env_refused_without_force(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")
    _make_venv(venvs_dir, "dev_no_runtime")

    handler = _make_handler(tmp_path, declared=["dev_workspace", "dev_no_runtime"])
    payload = remove_envs_action.RemoveEnvsRunPayload(env_names=["dev_no_runtime"])
    run_context = _make_run_context(payload)

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(payload, run_context)

    assert "dev_no_runtime" in str(exc_info.value)
    assert "force" in str(exc_info.value)


async def test_declared_env_accepted_with_force(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")
    _make_venv(venvs_dir, "dev_no_runtime")

    handler = _make_handler(tmp_path, declared=["dev_workspace", "dev_no_runtime"])
    payload = remove_envs_action.RemoveEnvsRunPayload(
        env_names=["dev_no_runtime"], force=True
    )
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs is not None
    assert [env.name for env in run_context.envs] == ["dev_no_runtime"]


async def test_current_env_refused_even_with_force(tmp_path: pathlib.Path) -> None:
    """The handler doing the removing lives in this env — deleting it would
    break the Extension Runner mid-run."""
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")

    handler = _make_handler(tmp_path, declared=["dev_workspace"])
    payload = remove_envs_action.RemoveEnvsRunPayload(
        env_names=["dev_workspace"], force=True
    )
    run_context = _make_run_context(payload)

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(payload, run_context)

    assert "dev_workspace" in str(exc_info.value)


async def test_guard_applies_even_when_venv_is_absent(tmp_path: pathlib.Path) -> None:
    """A rejection must not depend on whether the venv happens to be on disk
    right now, otherwise the same command succeeds or fails by accident."""
    (tmp_path / ".venvs").mkdir()

    handler = _make_handler(tmp_path, declared=["dev_workspace", "dev_no_runtime"])
    payload = remove_envs_action.RemoveEnvsRunPayload(env_names=["dev_no_runtime"])
    run_context = _make_run_context(payload)

    with pytest.raises(code_action.ActionFailedException):
        await handler.run(payload, run_context)


async def test_unknown_env_name_warns_user(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(
        tmp_path, declared=["dev_workspace"], user_messenger=user_messenger
    )
    payload = remove_envs_action.RemoveEnvsRunPayload(env_names=["typo_env"])
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs == []
    assert len(user_messenger.warnings) == 1
    assert "typo_env" in user_messenger.warnings[0]


async def test_unknown_env_name_stays_quiet_for_system_trigger(
    tmp_path: pathlib.Path,
) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(
        tmp_path, declared=["dev_workspace"], user_messenger=user_messenger
    )
    payload = remove_envs_action.RemoveEnvsRunPayload(env_names=["typo_env"])
    run_context = _make_run_context(payload, code_action.RunActionTrigger.SYSTEM)

    await handler.run(payload, run_context)

    assert user_messenger.warnings == []


async def test_no_orphans_does_not_warn(tmp_path: pathlib.Path) -> None:
    """Discovery finding nothing is not diagnosable — the caller named no
    specific items (R-505)."""
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(
        tmp_path, declared=["dev_workspace"], user_messenger=user_messenger
    )
    payload = remove_envs_action.RemoveEnvsRunPayload()
    run_context = _make_run_context(payload)

    await handler.run(payload, run_context)

    assert run_context.envs == []
    assert user_messenger.warnings == []
