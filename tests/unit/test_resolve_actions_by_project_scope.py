from __future__ import annotations

import pathlib

import pytest
from resolution_fake import SwappingResolver, make_preset_action

from finecode.wm_server import context, domain, errors
from finecode.wm_server import testing as wm_testing
from finecode.wm_server._api_handlers._helpers import _resolve_actions_by_project
from finecode.wm_server.services import project_resolution_service
from finecode.wm_server.services.run_service import proxy_utils


def _make_collected(path: pathlib.Path) -> domain.CollectedProject:
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


def _make_root_and_sibling(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, context.WorkspaceContext]:
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[root])
    ws_context.ws_projects[root] = _make_collected(root)
    ws_context.ws_projects[sibling] = _make_collected(sibling)
    return root, sibling, ws_context


def _build_ws_context_with_action(
    tmp_path: pathlib.Path, *, scope: domain.ActionScope | None
):
    """Build a single-project WorkspaceContext with one action of the given scope.

    ``canonical_source`` is pre-populated so scope resolution doesn't trigger a
    metadata round-trip through the (unconfigured) fake ER client.
    """
    action_source = "test.actions.TestAction"
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action", action_source=action_source
    )
    project.actions[0].canonical_source = action_source
    project.actions[0].scope = scope

    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return ws_context, action_source


async def test_explicit_project_rejected_for_workspace_scoped_action(
    tmp_path: pathlib.Path,
) -> None:
    """``runBatch`` must reject an explicit ``--project`` for a workspace-scoped
    action, mirroring the guard already enforced for single-action ``actions/run``
    (see ``_parse_and_validate_run_action_params``). Without this guard, passing
    any project (even the workspace root) silently dispatches the action hosted
    on the wrong project, which crashes deep inside handler execution instead of
    failing with a clear message.
    """
    ws_context, action_source = _build_ws_context_with_action(
        tmp_path, scope=domain.ActionScope.WORKSPACE
    )

    with pytest.raises(ValueError, match="workspace-scoped"):
        await _resolve_actions_by_project(
            project_names=[str(tmp_path)],
            action_sources=[action_source],
            ws_context=ws_context,
        )


async def test_explicit_project_accepted_for_project_scoped_action(
    tmp_path: pathlib.Path,
) -> None:
    """Control case: a normal project-scoped action must still resolve fine
    when an explicit ``--project`` is given."""
    ws_context, action_source = _build_ws_context_with_action(
        tmp_path, scope=domain.ActionScope.PROJECT
    )

    actions_by_project, name_to_source = await _resolve_actions_by_project(
        project_names=[str(tmp_path)],
        action_sources=[action_source],
        ws_context=ws_context,
    )

    assert actions_by_project == {tmp_path: ["test_action"]}
    assert name_to_source == {"test_action": action_source}


async def test_workspace_scoped_root_hosted_action_resolves_root_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose every action is workspace-scoped and hosted on the root
    resolves the root only: the sibling is never resolved (AC1, AC4)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="inspect_code",
                source="fine_inspect_code.InspectCodeAction",
                scope=domain.ActionScope.WORKSPACE,
            )
        ],
    )

    actions_by_project, _ = await _resolve_actions_by_project(
        None, ["fine_inspect_code.InspectCodeAction"], ws_context
    )

    assert resolver.calls == [[root]]
    assert actions_by_project == {root: ["inspect_code"]}


async def test_project_scoped_action_resolves_root_then_one_batch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project-scoped action resolves the root first, then all non-root
    projects in one batch, and pays no sibling metadata start for an action
    the root hosts (AC4)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="lint",
                source="fine_lint.LintAction",
                scope=domain.ActionScope.PROJECT,
            )
        ],
    )
    metadata_calls: list[tuple[str, pathlib.Path]] = []

    async def _recording_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        metadata_calls.append((action.name, project.dir_path))

    monkeypatch.setattr(
        proxy_utils, "ensure_action_metadata", _recording_ensure_metadata
    )

    actions_by_project, _ = await _resolve_actions_by_project(
        None, ["fine_lint.LintAction"], ws_context
    )

    assert resolver.calls == [[root], [sibling]]
    assert actions_by_project == {root: ["lint"], sibling: ["lint"]}
    assert metadata_calls == []


async def test_source_missing_from_root_falls_back_to_full_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An action the root does not host is still resolvable: the workspace is
    resolved and the sibling that declares the action is found."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    sibling_project = wm_testing.make_single_action_project(
        dir_path=sibling, action_name="tool", action_source="fine_tool.ToolAction"
    )
    sibling_project.actions[0].canonical_source = "fine_tool.ToolAction"
    sibling_project.actions[0].scope = domain.ActionScope.PROJECT
    # Re-register as a CollectedProject-object whose actions survive the swap.
    ws_context.ws_projects[sibling] = domain.CollectedProject(
        name=sibling_project.name,
        dir_path=sibling,
        def_path=sibling_project.def_path,
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs=sibling_project.env_configs,
        actions=sibling_project.actions,
        services=sibling_project.services,
        action_handler_configs=sibling_project.action_handler_configs,
    )
    resolver = SwappingResolver(ws_context, monkeypatch)

    actions_by_project, _ = await _resolve_actions_by_project(
        None, ["fine_tool.ToolAction"], ws_context
    )

    assert resolver.calls == [[root], [sibling]]
    assert actions_by_project == {sibling: ["tool"]}


async def test_root_metadata_failure_propagates_without_full_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root action whose metadata cannot be resolved propagates that failure
    instead of falling back to full resolution — its scope is unknown either
    way (C step 3)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="x",
                source="fine_x.XAction",
                scope=domain.ActionScope.WORKSPACE,
                no_metadata=True,
            )
        ],
    )

    async def _raising_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        raise errors.ActionNotResolvableError("cannot resolve")

    monkeypatch.setattr(proxy_utils, "ensure_action_metadata", _raising_ensure_metadata)

    with pytest.raises(errors.ActionNotResolvableError):
        await _resolve_actions_by_project(None, ["fine_x.XAction"], ws_context)

    assert resolver.calls == [[root]]


async def test_root_failure_raises_without_further_calls(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the root itself fails to resolve, the run fails naming it — nothing
    else is resolved."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context, monkeypatch, fail_paths={root}, fail_message="root venv missing"
    )

    with pytest.raises(project_resolution_service.ProjectResolutionFailed):
        await _resolve_actions_by_project(None, ["fine_test.ScopeAction"], ws_context)

    assert resolver.calls == [[root]]
