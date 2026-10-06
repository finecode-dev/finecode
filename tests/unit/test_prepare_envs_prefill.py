from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.services import action_meta_cache, prepare_envs_service


def _make_project(dir_path: pathlib.Path) -> domain.CollectedProject:
    action = domain.Action(
        name="a",
        source="pkg.AAction",
        handlers=[
            domain.ActionHandler(
                name="h", source="pkg.H", config={}, env="env_a", dependencies=[]
            )
        ],
        config={},
    )
    return domain.CollectedProject(
        name="p",
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[action],
        services=[],
        action_handler_configs={},
    )


async def test_prefill_waits_skips_and_applies_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prefill waits out the racy window, skips envs with no actions, and never touches domain objects."""
    project = _make_project(tmp_path)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    sleeps: list = []
    seen: list = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def _recording_lookup(
        project_arg, env_name, sources, ws_context_arg, **kwargs
    ):
        seen.append((env_name, sources, kwargs.get("use_memo")))
        return action_meta_cache.LookupResult(
            metas={sources[0]: {"canonical_source": "x"}}, failures={}
        )

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(action_meta_cache, "lookup", _recording_lookup)
    await action_meta_cache.prefill(
        project, ["env_a", "env_empty", "dev_workspace"], ws_context
    )

    assert sleeps == [action_meta_cache.RACY_WINDOW_SEC]
    assert [env for env, _, _ in seen] == ["env_a"]
    assert seen[0][2] is False
    assert project.actions[0].canonical_source is None


async def test_prefill_failure_returns_normally_with_info(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed prefill never fails the install; one line says it will resolve on use."""
    from loguru import logger

    project = _make_project(tmp_path)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    records: list = []

    async def _no_wait(delay: float) -> None:
        return None

    async def _boom(project_arg, env_name, sources, ws_context_arg, **kwargs):
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    monkeypatch.setattr(action_meta_cache, "lookup", _boom)
    sink_id = logger.add(lambda message: records.append(message.record["message"]))
    try:
        await action_meta_cache.prefill(project, ["env_a"], ws_context)
    finally:
        logger.remove(sink_id)

    assert any("env_a" in message for message in records)


async def test_prepare_envs_finally_forgets_failures(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memo entries never survive a prepare-envs run, however it ends."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    venv = tmp_path / ".venvs" / "env_a"
    ws_context.action_meta_failures[(venv, None)] = context.ActionMetaFailure(
        kind="timeout", reason="t", stamps=(), site_packages_mtime_ns=None
    )

    async def _ok(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(prepare_envs_service, "_prepare_envs_impl", _ok)
    await prepare_envs_service.prepare_envs(ws_context, tmp_path)
    assert ws_context.action_meta_failures == {}

    ws_context.action_meta_failures[(venv, None)] = context.ActionMetaFailure(
        kind="timeout", reason="t", stamps=(), site_packages_mtime_ns=None
    )

    async def _failing(*args, **kwargs) -> None:
        raise prepare_envs_service.PrepareEnvsFailed("boom")

    monkeypatch.setattr(prepare_envs_service, "_prepare_envs_impl", _failing)
    with pytest.raises(prepare_envs_service.PrepareEnvsFailed):
        await prepare_envs_service.prepare_envs(ws_context, tmp_path)
    assert ws_context.action_meta_failures == {}


async def test_install_env_for_project_clears_its_venv_memo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful install invalidates that venv's memo, so the next query refills it."""
    project = _make_project(tmp_path)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = project
    ws_context.ws_projects_raw_configs[tmp_path] = {}
    venv = tmp_path / ".venvs" / "env_a"
    failure = context.ActionMetaFailure(
        kind="timeout", reason="t", stamps=(), site_packages_mtime_ns=None
    )
    ws_context.action_meta_failures[(venv, None)] = failure

    async def _ok_start(projects, ws_context_arg, **kwargs) -> None:
        return None

    async def _ok_env_action(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "finecode.wm_server.services.runner_start_service.start_runners_with_auto_prepare",
        _ok_start,
    )
    monkeypatch.setattr(prepare_envs_service, "_run_env_action", _ok_env_action)
    await prepare_envs_service.install_env_for_project(project, "env_a", ws_context)

    assert (venv, None) not in ws_context.action_meta_failures
