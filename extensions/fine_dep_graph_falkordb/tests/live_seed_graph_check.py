"""Explicit live graph check for issue 93 (not default-discovered).

Run explicitly via file selection; a missing FalkorDB service fails, never skips.
Uses an isolated UUID-named graph and cleans only its own nodes.
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from unittest import mock

import falkordb
from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoRunResult,
)
from fine_dep_graph.seed_workspace_dependency_graph_action import (
    SeedWorkspaceDependencyGraphAction,
    SeedWorkspaceDependencyGraphRunContext,
    SeedWorkspaceDependencyGraphRunPayload,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)

from fine_dep_graph_falkordb.seed_workspace_dependency_graph_handler import (
    SeedWorkspaceDependencyGraphHandler,
)

_MANIFEST = [
    "fine_agent",
    "fine_audit_code",
    "fine_check_imports",
    "fine_code_hierarchy",
    "fine_dep_graph",
    "fine_dist_artifacts",
    "fine_docs",
    "fine_docs_markdown",
    "fine_envs",
    "fine_format",
    "fine_git",
    "fine_git_hooks",
    "fine_inlay_hints",
    "fine_inspect_code",
    "fine_lint",
    "fine_logs",
    "fine_python_check_imports",
    "fine_python_code_hierarchy",
    "fine_python_envs",
    "fine_python_format",
    "fine_python_inlay_hints",
    "fine_python_lint",
    "fine_python_recommended",
    "fine_python_semantic_tokens",
    "fine_python_src_artifacts",
    "fine_python_symbol_info",
    "fine_python_test",
    "fine_python_type_check",
    "fine_release",
    "fine_semantic_tokens",
    "fine_src_artifacts",
    "fine_symbol_info",
    "fine_system_setup",
    "fine_test",
    "fine_toml_recommended",
    "fine_type_check",
    "fine_wal_events",
]


def _write_pkg(pkg_dir: pathlib.Path, name: str) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    _ = (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "' + name + '"\n', encoding="utf-8"
    )


def _build_layout(root: pathlib.Path, *, new_layout: bool) -> list[WorkspaceProject]:
    projects: list[WorkspaceProject] = []
    for name in _MANIFEST:
        sub = "extensions" if new_layout else "presets"
        pkg_dir = root / sub / name
        _write_pkg(pkg_dir, name)
        projects.append(
            WorkspaceProject(path=pkg_dir, config_status=ProjectConfigStatus.VALID)
        )
    for name, sub in [
        ("finecode", "."),
        ("fine_python_ruff", "extensions"),
        ("fine_knowledge", "presets"),
    ]:
        pkg_dir = root / sub / name if sub != "." else root / name
        _write_pkg(pkg_dir, name)
        projects.append(
            WorkspaceProject(path=pkg_dir, config_status=ProjectConfigStatus.VALID)
        )
    return projects


class _LiveGraphProvider:
    def __init__(self, graph: falkordb.Graph) -> None:
        self._graph = graph

    def get_client(self) -> falkordb.FalkorDB:
        raise NotImplementedError

    def get_graph(self) -> falkordb.Graph:
        return self._graph


def _seed(
    projects: list[WorkspaceProject],
    graph: falkordb.Graph,
    *,
    clear_existing: bool,
) -> None:
    workspace_info_provider = mock.create_autospec(
        iworkspaceinfoprovider.IWorkspaceInfoProvider, instance=True
    )
    workspace_info_provider.get_workspace_projects.return_value = projects
    workspace_action_runner = mock.create_autospec(
        iworkspaceactionrunner.IWorkspaceActionRunner, instance=True
    )
    workspace_action_runner.run_action_in_projects.return_value = dict[
        pathlib.Path, CollectProjectDependencyInfoRunResult
    ]()
    run_context = mock.create_autospec(
        SeedWorkspaceDependencyGraphRunContext, instance=True
    )
    run_context.meta = code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )
    handler = SeedWorkspaceDependencyGraphHandler(
        falkordb_client_provider=_LiveGraphProvider(graph),
        workspace_action_runner=workspace_action_runner,
        workspace_info_provider=workspace_info_provider,
    )
    payload = SeedWorkspaceDependencyGraphRunPayload(clear_existing=clear_existing)
    _ = asyncio.run(handler.run(payload, run_context))


def _node_map(graph: falkordb.Graph) -> dict[str, str]:
    result = graph.query("MATCH (p:Package) RETURN p.name, p.kind")
    return {str(row[0]): str(row[1]) for row in result.result_set}


def _preset_names(graph: falkordb.Graph) -> list[str]:
    result = graph.query("MATCH (p:Package {kind: 'preset'}) RETURN p.name")
    return sorted(str(row[0]) for row in result.result_set)


def test_live_seed_graph_reports_same_preset_kinds(tmp_path: pathlib.Path) -> None:
    graph_name = f"test_93_{uuid.uuid4().hex}"
    try:
        client = falkordb.FalkorDB(host="falkordb", port=6379)
        graph = client.select_graph(graph_name)
        _ = graph.query("MATCH (n) RETURN count(n)")
    except Exception as exc:
        raise AssertionError(
            f"FalkorDB at falkordb:6379 is unavailable: {exc}"
        ) from exc

    try:
        old_projects = _build_layout(tmp_path / "old", new_layout=False)
        _seed(old_projects, graph, clear_existing=True)
        fresh = _node_map(graph)
        assert len(fresh) == len(_MANIFEST) + 3
        for name in _MANIFEST:
            assert fresh[name] == "preset"
        fresh_preset_query = _preset_names(graph)
        assert set(_MANIFEST) <= set(fresh_preset_query)

        _seed(old_projects, graph, clear_existing=False)
        incremental = _node_map(graph)
        assert incremental == fresh
        assert _preset_names(graph) == fresh_preset_query

        _seed(old_projects, graph, clear_existing=True)
        reseeded = _node_map(graph)
        assert reseeded == fresh

        new_projects = _build_layout(tmp_path / "new", new_layout=True)
        _seed(new_projects, graph, clear_existing=True)
        moved = _node_map(graph)
        assert moved == fresh
        assert _preset_names(graph) == fresh_preset_query

        counts = graph.query("MATCH (p:Package) RETURN p.name, count(p)")
        by_name: dict[str, int] = {}
        for row in counts.result_set:
            by_name[str(row[0])] = int(row[1])
        for name in _MANIFEST:
            assert by_name[name] == 1
    finally:
        _ = graph.query("MATCH (n) DETACH DELETE n")


def test_collect_result_carries_legacy_kind() -> None:
    result = CollectProjectDependencyInfoRunResult(
        package_name="fine_format", package_kind="preset"
    )
    assert result.package_kind == "preset"
    assert SeedWorkspaceDependencyGraphAction.__name__ == (
        "SeedWorkspaceDependencyGraphAction"
    )
