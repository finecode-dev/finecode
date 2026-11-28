"""The remaining WM entry-point gates (D).

Listing (`actions/list`), schema fetch, raw-config reads and the action tree all
resolve the projects they read on demand, so a lazily attached WM never shows a
partial action set and never silently skips a project that failed to resolve.
"""

from __future__ import annotations

import pathlib

import pytest
from resolution_fake import SwappingResolver, make_preset_action

from finecode.wm_server import context, domain, errors
from finecode.wm_server._api_handlers._actions import _handle_get_payload_schemas
from finecode.wm_server._api_handlers._workspace import (
    _handle_get_project_raw_config,
    _handle_list_actions,
)
from finecode.wm_server.runner import run_dispatch_bridge
from finecode.wm_server.services import action_tree, run_service
from finecode.wm_server.services.run_service import find_all_projects_with_action


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
    sibling = root / "sibling"  # nested under the root so the tree nests it
    ws_context = context.WorkspaceContext(ws_dirs_paths=[root])
    ws_context.ws_projects[root] = _make_collected(root)
    ws_context.ws_projects[sibling] = _make_collected(sibling)
    return root, sibling, ws_context


_AUDIT_ACTION = make_preset_action(
    name="audit",
    source="fine_test.AuditAction",
    scope=domain.ActionScope.PROJECT,
)


async def test_find_all_projects_with_action_resolves_siblings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``finecode`` fan-out reaches a sibling that was unresolved beforehand
    (AC3)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    sibling_project = _make_collected(sibling)
    sibling_project.actions.append(_AUDIT_ACTION)
    ws_context.ws_projects[sibling] = sibling_project
    resolver = SwappingResolver(ws_context, monkeypatch)

    paths = await find_all_projects_with_action("audit", ws_context)

    assert sibling in paths
    assert resolver.calls == [[root, sibling]]


async def test_find_all_projects_with_action_fails_loudly_on_failing_sibling(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling that fails to resolve fails the enumeration, naming it —
    never a silently partial fan-out (AC3)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context, monkeypatch, fail_paths={sibling}, fail_message="venv missing"
    )

    with pytest.raises(run_service.ActionRunFailed) as excinfo:
        await find_all_projects_with_action("audit", ws_context)

    assert "venv missing" in str(excinfo.value)


async def test_list_actions_names_mode_resolves_the_root_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_actions(names=[...])`` for a workspace-scoped root-hosted action
    resolves the root alone (AC6b)."""
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

    result = await _handle_list_actions({"names": ["inspect_code"]}, ws_context)

    assert resolver.calls == [[root]]
    assert [a["name"] for a in result["actions"]] == ["inspect_code"]
    assert result["unresolvedProjects"] == []


async def test_unfiltered_list_actions_shows_preset_action_and_failures(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unfiltered listing returns a sibling's preset action and reports a
    failing sibling in ``unresolvedProjects`` (AC6b)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[_AUDIT_ACTION],
        fail_paths={sibling},
        fail_message="venv missing",
    )

    result = await _handle_list_actions({}, ws_context)

    names = {a["name"] for a in result["actions"]}
    assert "audit" in names
    unresolved_by_project = {u["project"] for u in result["unresolvedProjects"]}
    assert str(sibling) in unresolved_by_project
    assert resolver.calls == [[root, sibling]]


async def test_list_actions_rejects_project_and_projects_together(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both ``project`` and ``projects`` contradicts itself and is an
    error."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    SwappingResolver(ws_context, monkeypatch)

    with pytest.raises(ValueError, match="not both"):
        await _handle_list_actions(
            {"project": str(root), "projects": [str(sibling)]}, ws_context
        )


async def test_payload_schemas_resolves_only_the_named_project(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``getPayloadSchemas`` naming project X resolves X and nothing else on a
    lazily attached WM (AC6a)."""
    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_collected(a)
    resolver = SwappingResolver(ws_context, monkeypatch, preset_actions=[_AUDIT_ACTION])

    result = await _handle_get_payload_schemas(
        {"project": str(a), "actionSources": ["fine_test.AuditAction"]}, ws_context
    )

    assert resolver.calls == [[a]]
    assert "fine_test.AuditAction" in result["schemas"]


async def test_get_project_raw_config_is_post_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``getProjectRawConfig`` on a lazily attached WM returns the preset-merged
    config, i.e. the one the resolution wrote (AC14)."""
    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_collected(a)
    ws_context.ws_projects_raw_configs[a] = {"sentinel": "raw"}
    resolver = SwappingResolver(ws_context, monkeypatch)

    result = await _handle_get_project_raw_config({"project": str(a)}, ws_context)

    assert result["rawConfig"] == {"tool": {"finecode": {}}}
    assert resolver.calls == [[a]]


async def test_tree_shows_sibling_preset_action_and_resolution_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The action tree holds a sibling's preset action, and a failing sibling's
    node carries an additive ``resolutionError``."""
    root = tmp_path / "root"
    ok_sibling = root / "ok"
    bad_sibling = root / "bad"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[root])
    ws_context.ws_projects[root] = _make_collected(root)
    ws_context.ws_projects[ok_sibling] = _make_collected(ok_sibling)
    ws_context.ws_projects[bad_sibling] = _make_collected(bad_sibling)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[_AUDIT_ACTION],
        fail_paths={bad_sibling},
        fail_message="venv missing",
    )

    result = await action_tree._handle_get_tree(None, ws_context)

    def _flatten(nodes: list[dict]) -> list[dict]:
        for node in nodes:
            yield node
            yield from _flatten(node.get("subnodes") or [])

    nodes = list(_flatten(result["nodes"]))
    ok_node = next(n for n in nodes if n.get("nodeId") == ok_sibling.as_posix())
    ok_actions_group = next(n for n in ok_node["subnodes"] if n.get("nodeType") == 3)
    action_names = {n["name"] for n in ok_actions_group["subnodes"]}
    # The resolved sibling's preset action survives in the tree ...
    assert action_names == {"audit"}
    # ... and the failing sibling's node names why it could not be shown fully.
    bad_node = next(n for n in nodes if n.get("nodeId") == bad_sibling.as_posix())
    assert bad_node["resolutionError"] == "venv missing"


async def test_list_workspace_actions_includes_sibling_preset_action(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``finecode/listWorkspaceActions`` on a lazily attached WM returns a
    sibling's preset action — the registry facts are complete (AC7)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(ws_context, monkeypatch, preset_actions=[_AUDIT_ACTION])
    handlers = run_dispatch_bridge.handlers()
    assert handlers is not None

    result = await handlers.list_workspace_actions(ws_context)

    sources = {a["source"] for a in result["actions"]}
    assert "fine_test.AuditAction" in sources
    # Enumerating callers resolve every valid project in one batch.
    assert resolver.calls == [[root, sibling]]


async def test_list_workspace_actions_fails_naming_a_failing_sibling(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling that fails to resolve fails the registry extraction, naming it
    (AC7)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context, monkeypatch, fail_paths={sibling}, fail_message="venv missing"
    )
    handlers = run_dispatch_bridge.handlers()
    assert handlers is not None

    with pytest.raises(errors.ProjectError) as excinfo:
        await handlers.list_workspace_actions(ws_context)

    assert "venv missing" in str(excinfo.value)
