from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.services.run_service import (
    ProjectExecutor,
    exceptions,
    proxy_utils,
)


def _build_session(tmp_path: pathlib.Path):
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return client, project, ws_context


async def test_run_action_retries_metadata_resolution_when_canonical_source_unresolved(
    tmp_path: pathlib.Path,
) -> None:
    """An action may still be unresolved (``canonical_source is None``) when a
    run is requested for it, because no prior request has started the env
    that hosts its handlers. ``run_action`` must attempt to resolve it via
    ``ensure_action_metadata`` before concluding the action doesn't exist.
    """
    client, project, ws_context = _build_session(tmp_path)
    action_source = project.actions[0].source
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {}}
        )
    )

    async def _fake_ensure_action_metadata(action, project_arg, ws_context_arg):
        action.canonical_source = action_source  # simulate the env resolving it

    async def _empty_resolve(*args, **kwargs):
        return {}

    with (
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=_fake_ensure_action_metadata,
        ),
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_empty_resolve,
        ),
    ):
        result = await ProjectExecutor(ws_context).run_action(
            action_source=action_source,
            params={},
            project_path=project.dir_path,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            origin=None,
        )

    assert result.return_code == 0


async def test_run_action_still_fails_when_metadata_cannot_resolve(
    tmp_path: pathlib.Path,
) -> None:
    """When metadata resolution cannot recover a matching action (the source
    genuinely doesn't exist in this project), the original "no such action"
    failure must still surface, unmasked.
    """
    _client, project, ws_context = _build_session(tmp_path)

    async def _noop_ensure_action_metadata(action, project_arg, ws_context_arg):
        return None  # canonical_source stays unresolved

    async def _empty_resolve(*args, **kwargs):
        return {}

    with (
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=_noop_ensure_action_metadata,
        ),
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_empty_resolve,
        ),
        pytest.raises(exceptions.ActionRunFailed),
    ):
        await ProjectExecutor(ws_context).run_action(
            action_source="does.not.Exist",
            params={},
            project_path=project.dir_path,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            origin=None,
        )


def _make_multi_env_project(tmp_path: pathlib.Path):
    from finecode.wm_server import domain

    def _action(name: str, source: str, env: str) -> domain.Action:
        action = domain.Action(
            name=name,
            source=source,
            handlers=[
                domain.ActionHandler(
                    name=f"{name}_h",
                    source=f"{source}Handler",
                    config={},
                    env=env,
                    dependencies=[],
                )
            ],
            config={},
        )
        return action

    target = _action("target", "pkg.TargetAction", "env_target")
    others = [
        _action("o1", "pkg.OtherOne", "env_a"),
        _action("o2", "pkg.OtherTwo", "env_b"),
        _action("o3", "pkg.OtherThree", "env_c"),
    ]
    collected = domain.CollectedProject(
        name="p",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[target, *others],
        services=[],
        action_handler_configs={},
    )
    return domain.ResolvedProject.from_collected(collected), target


async def test_run_action_dispatches_after_cache_resolves_target(
    tmp_path: pathlib.Path,
) -> None:
    """A cache-resolved target dispatches without starting any runner, so nested runs stay cheap."""
    from finecode.wm_server.runner import runner_manager

    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(
        working_dir_path=tmp_path, env_name="env_target", client=client
    )
    project, target = _make_multi_env_project(tmp_path)
    ws_context = wm_testing.make_workspace_context(
        project=project, runner=runner, env_name="env_target"
    )
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {}}
        )
    )
    canonical = "pkg.impl.TargetAction"

    async def _resolving_cache(project_arg, ws_context_arg, **kwargs):
        target.canonical_source = canonical
        target.parent_action_source = None
        target.language = None
        return {}

    with (
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_resolving_cache,
        ),
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
    ):
        result = await ProjectExecutor(ws_context).run_action(
            action_source=canonical,
            params={},
            project_path=project.dir_path,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            origin=None,
        )
    assert result.return_code == 0


async def test_needed_fallback_runs_before_dumping_other_envs(
    tmp_path: pathlib.Path,
) -> None:
    """A tail candidate that only needs its env started resolves without dumping every other env."""
    from finecode.wm_server import context as wm_context
    from finecode.wm_server import domain

    candidate = domain.Action(
        name="target",
        source="pkg.TargetAction",
        handlers=[
            domain.ActionHandler(
                name="h",
                source="pkg.TargetHandler",
                config={},
                env="env_target",
                dependencies=[],
            )
        ],
        config={},
    )
    other = domain.Action(
        name="other",
        source="pkg.OtherAction",
        handlers=[
            domain.ActionHandler(
                name="h",
                source="pkg.OtherHandler",
                config={},
                env="env_other",
                dependencies=[],
            )
        ],
        config={},
    )
    project = domain.CollectedProject(
        name="p",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[candidate, other],
        services=[],
        action_handler_configs={},
    )
    ws_context = wm_context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    canonical = "pkg.impl.TargetAction"

    for kind in ("skew", "env_unusable", "timeout", None):
        candidate.canonical_source = None
        other.canonical_source = None
        resolve_calls: list = []

        async def _cache_first(project_arg, ws_context_arg, **kwargs):
            resolve_calls.append(kwargs.get("actions"))
            if len(resolve_calls) == 1:
                failure = None
                if kind is not None:
                    failure = wm_context.ActionMetaFailure(
                        kind=kind,
                        reason=f"{kind} boom",
                        stamps=(),
                        site_packages_mtime_ns=None,
                    )
                return {candidate.source: failure}
            raise AssertionError("must not dump other envs once the target resolves")

        async def _resolving_ensure(action, project_arg, ws_context_arg):
            action.canonical_source = canonical

        with (
            mock.patch.object(
                proxy_utils.action_meta_cache,
                "resolve_unresolved",
                side_effect=_cache_first,
            ),
            mock.patch.object(
                proxy_utils, "ensure_action_metadata", side_effect=_resolving_ensure
            ),
        ):
            found = await proxy_utils.find_action_by_canonical_source(
                canonical, project, ws_context
            )
        assert found is candidate
        assert len(resolve_calls) == 1


async def test_import_failed_candidate_names_its_error(
    tmp_path: pathlib.Path,
) -> None:
    """An unimportable candidate never starts its env, and the failure names why."""
    from finecode.wm_server import context as wm_context
    from finecode.wm_server import domain

    candidate = domain.Action(
        name="target",
        source="pkg.TargetAction",
        handlers=[
            domain.ActionHandler(
                name="h",
                source="pkg.TargetHandler",
                config={},
                env="env_target",
                dependencies=[],
            )
        ],
        config={},
    )
    project = domain.CollectedProject(
        name="p",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[candidate],
        services=[],
        action_handler_configs={},
    )
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)

    async def _import_failed_cache(project_arg, ws_context_arg, **kwargs):
        ws_context.action_meta_failures[
            (tmp_path / ".venvs" / "env_target", candidate.source)
        ] = wm_context.ActionMetaFailure(
            kind="import_failed",
            reason="ImportError: no module named pkg",
            stamps=(),
            site_packages_mtime_ns=None,
        )
        return {
            candidate.source: wm_context.ActionMetaFailure(
                kind="import_failed",
                reason="ImportError: no module named pkg",
                stamps=(),
                site_packages_mtime_ns=None,
            )
        }

    with (
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_import_failed_cache,
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
        pytest.raises(exceptions.ActionRunFailed, match="no module named pkg"),
    ):
        await ProjectExecutor(ws_context).run_action(
            action_source="pkg.impl.TargetAction",
            params={},
            project_path=project.dir_path,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
            origin=None,
        )
