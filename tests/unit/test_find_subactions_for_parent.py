from __future__ import annotations

import pathlib
from unittest import mock

from finecode.wm_server import domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services.run_service import proxy_utils

_PARENT_SOURCE = "fine_check_imports.check_imports_action.CheckImportsAction"


def _make_action(
    *,
    name: str,
    source: str,
    handler_env: str = "dev_no_runtime",
    canonical_source: str | None = None,
    parent_action_source: str | None = None,
    language: str | None = None,
) -> domain.Action:
    action = domain.Action(
        name=name,
        source=source,
        handlers=[
            domain.ActionHandler(
                name=f"{name}_handler",
                source=f"{source}Handler",
                config={},
                env=handler_env,
                dependencies=[],
            )
        ],
        config={},
    )
    action.canonical_source = canonical_source
    action.parent_action_source = parent_action_source
    action.language = language
    return action


def _make_project(
    dir_path: pathlib.Path, actions: list[domain.Action]
) -> domain.CollectedProject:
    return domain.CollectedProject(
        name="test_project",
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={
            "dev_no_runtime": domain.EnvConfig(
                runner_config=domain.RunnerConfig(debug=False)
            )
        },
        actions=actions,
        services=[],
        action_handler_configs={},
    )


async def test_find_subactions_for_parent_returns_already_resolved_matches(
    tmp_path: pathlib.Path,
) -> None:
    """The common case: the subaction's metadata was already resolved when
    its own env's ER started — no on-demand resolution is needed, just a
    filter over what's cached."""
    python_subaction = _make_action(
        name="check_python_imports",
        source="fine_python_lang.CheckPythonImportsAction",
        canonical_source="fine_python_lang.check_python_imports_action.CheckPythonImportsAction",
        parent_action_source=_PARENT_SOURCE,
        language="python",
    )
    unrelated_action = _make_action(
        name="format_python",
        source="fine_python_lang.FormatPythonAction",
        canonical_source="fine_python_lang.format_python_action.FormatPythonAction",
        parent_action_source="fine_format.format_action.FormatAction",
        language="python",
    )
    project = _make_project(tmp_path, [python_subaction, unrelated_action])
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_no_runtime"
        ),
        env_name="dev_no_runtime",
    )

    result = await proxy_utils.find_subactions_for_parent(
        _PARENT_SOURCE, project, ws_context
    )

    assert result == [python_subaction]


async def test_find_subactions_for_parent_resolves_unresolved_actions_on_demand(
    tmp_path: pathlib.Path,
) -> None:
    """An action not yet imported by any ER (canonical_source is None) must
    still be discoverable: the cache or dump resolves it without starting
    any runner, otherwise discovery would silently depend on which envs
    happened to have started already."""
    python_subaction = _make_action(
        name="check_python_imports",
        source="fine_python_lang.CheckPythonImportsAction",
        handler_env="dev_no_runtime",
    )
    project = _make_project(tmp_path, [python_subaction])
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )

    async def _fake_resolve_unresolved(project, ws_context, **_):
        python_subaction.canonical_source = "resolved." + python_subaction.source
        python_subaction.parent_action_source = _PARENT_SOURCE
        python_subaction.language = "python"
        python_subaction.meta_from_cache = True
        return {}

    with (
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_fake_resolve_unresolved,
        ),
    ):
        result = await proxy_utils.find_subactions_for_parent(
            _PARENT_SOURCE, project, ws_context
        )

    assert result == [python_subaction]


async def test_find_subactions_for_parent_skips_actions_that_cannot_be_resolved(
    tmp_path: pathlib.Path,
) -> None:
    """A best-effort query: one action's env failing to dump must not fail
    the whole discovery — it's simply not a candidate."""
    broken_action = _make_action(
        name="check_python_imports",
        source="fine_python_lang.CheckPythonImportsAction",
        handler_env="dev_no_runtime",
    )
    project = _make_project(tmp_path, [broken_action])
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )

    async def _failing_resolve(project, ws_context, **_):
        from finecode.wm_server import context as wm_context

        return {
            broken_action.source: wm_context.ActionMetaFailure(
                kind="env_unusable",
                reason="boom",
                stamps=(),
                site_packages_mtime_ns=None,
            )
        }

    with (
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_failing_resolve,
        ),
    ):
        result = await proxy_utils.find_subactions_for_parent(
            _PARENT_SOURCE, project, ws_context
        )

    assert result == []


async def test_find_subactions_for_parent_excludes_matches_without_a_language(
    tmp_path: pathlib.Path,
) -> None:
    """A resolved parent_action_source without a language tag is not a valid
    language specialization (ADR-0008) and must not be returned as one."""
    action_without_language = _make_action(
        name="not_a_language_subaction",
        source="fine_check_imports.SomeOtherChildAction",
        canonical_source="fine_check_imports.some_other_child_action.SomeOtherChildAction",
        parent_action_source=_PARENT_SOURCE,
        language=None,
    )
    project = _make_project(tmp_path, [action_without_language])
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_no_runtime"
        ),
        env_name="dev_no_runtime",
    )

    result = await proxy_utils.find_subactions_for_parent(
        _PARENT_SOURCE, project, ws_context
    )

    assert result == []


async def test_find_subactions_never_starts_a_runner(
    tmp_path: pathlib.Path,
) -> None:
    """Opportunistic discovery resolves across envs without starting anything.

    Subactions in two unstarted envs resolve through the cache or dump and
    are returned; one that still fails is excluded, so a workspace-wide
    format never pays for envs no action executes.
    """
    from finecode.wm_server import context as wm_context

    python_subaction = _make_action(
        name="check_python_imports",
        source="fine_python_lang.CheckPythonImportsAction",
        handler_env="dep_graph",
    )
    toml_subaction = _make_action(
        name="check_toml",
        source="fine_toml_lang.CheckTomlAction",
        handler_env="wal_explorer",
    )
    broken_action = _make_action(
        name="check_broken",
        source="fine_broken.CheckBrokenAction",
        handler_env="dev",
    )
    project = _make_project(tmp_path, [python_subaction, toml_subaction, broken_action])
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )

    async def _fake_resolve_unresolved(project, ws_context, **_):
        for action in (python_subaction, toml_subaction):
            action.canonical_source = "resolved." + action.source
            action.parent_action_source = _PARENT_SOURCE
            action.language = "python"
            action.meta_from_cache = True
        return {
            broken_action.source: wm_context.ActionMetaFailure(
                kind="env_unusable",
                reason="boom",
                stamps=(),
                site_packages_mtime_ns=None,
            )
        }

    with (
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_fake_resolve_unresolved,
        ),
    ):
        result = await proxy_utils.find_subactions_for_parent(
            _PARENT_SOURCE, project, ws_context
        )

    assert result == [python_subaction, toml_subaction]
