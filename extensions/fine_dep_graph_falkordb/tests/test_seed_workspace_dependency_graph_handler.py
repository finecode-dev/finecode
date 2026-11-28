from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoRunContext,
    CollectProjectDependencyInfoRunPayload,
    CollectProjectDependencyInfoRunResult,
)
from fine_dep_graph.pyproject_package_info_handler import PyprojectPackageInfoHandler
from fine_dep_graph.seed_workspace_dependency_graph_action import (
    SeedWorkspaceDependencyGraphRunContext,
    SeedWorkspaceDependencyGraphRunPayload,
    SeedWorkspaceDependencyGraphRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectinfoprovider,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)

from fine_dep_graph_falkordb.ifalkordb_client_provider import IFalkorDBClientProvider
from fine_dep_graph_falkordb.seed_workspace_dependency_graph_handler import (
    SeedWorkspaceDependencyGraphHandler,
)


class _RecordingGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def query(self, query: str, params: dict[str, str] | None = None) -> None:
        self.calls.append((query, dict(params or {})))


def _write_pyproject(pkg_dir: pathlib.Path, name: str) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    _ = (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "' + name + '"\n', encoding="utf-8"
    )


def _context(
    payload: SeedWorkspaceDependencyGraphRunPayload,
) -> SeedWorkspaceDependencyGraphRunContext:
    return SeedWorkspaceDependencyGraphRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=code_action.RunContextInfoProvider(is_concurrent_execution=False),
    )


def _run(
    handler: SeedWorkspaceDependencyGraphHandler,
    payload: SeedWorkspaceDependencyGraphRunPayload,
) -> SeedWorkspaceDependencyGraphRunResult:
    return asyncio.run(handler.run(payload, _context(payload)))


def _make_handler(
    projects: list[WorkspaceProject],
    results: dict[pathlib.Path, CollectProjectDependencyInfoRunResult],
    graph: _RecordingGraph,
) -> SeedWorkspaceDependencyGraphHandler:
    workspace_info_provider = mock.create_autospec(
        iworkspaceinfoprovider.IWorkspaceInfoProvider, instance=True
    )
    workspace_info_provider.get_workspace_projects.return_value = projects
    workspace_action_runner = mock.create_autospec(
        iworkspaceactionrunner.IWorkspaceActionRunner, instance=True
    )
    workspace_action_runner.run_action_in_projects.return_value = results
    client_provider = mock.create_autospec(IFalkorDBClientProvider, instance=True)
    client_provider.get_graph.return_value = graph
    return SeedWorkspaceDependencyGraphHandler(
        falkordb_client_provider=client_provider,
        workspace_action_runner=workspace_action_runner,
        workspace_info_provider=workspace_info_provider,
    )


def test_merge_args_cover_named_and_non_actionable_projects(
    tmp_path: pathlib.Path,
) -> None:
    valid_dir = tmp_path / "presets" / "fine_format"
    _write_pyproject(valid_dir, "fine_format")
    retained_dir = tmp_path / "presets" / "fine_knowledge"
    _write_pyproject(retained_dir, "fine_knowledge")
    missing_dir = tmp_path / "presets" / "empty_pkg"
    missing_dir.mkdir(parents=True, exist_ok=True)

    projects = [
        WorkspaceProject(path=valid_dir, config_status=ProjectConfigStatus.VALID),
        WorkspaceProject(
            path=retained_dir, config_status=ProjectConfigStatus.NO_CONFIG
        ),
        WorkspaceProject(path=missing_dir, config_status=ProjectConfigStatus.VALID),
    ]
    results: dict[pathlib.Path, CollectProjectDependencyInfoRunResult] = {
        valid_dir: CollectProjectDependencyInfoRunResult(
            package_name="fine_format",
            package_kind="preset",
            pyproject_dependencies=[],
            used_preset_sources=[],
        ),
        missing_dir: CollectProjectDependencyInfoRunResult(),
    }
    graph = _RecordingGraph()
    handler = _make_handler(projects, results, graph)

    result = _run(handler, SeedWorkspaceDependencyGraphRunPayload(clear_existing=True))

    merges = [params for query, params in graph.calls if query.startswith("MERGE")]
    assert result.packages_indexed == 2
    assert {params["name"]: params["kind"] for params in merges} == {
        "fine_format": "preset",
        "fine_knowledge": "preset",
    }
    assert any(
        "is skipped" in warning or "no package name" in warning
        for warning in result.warnings
    )


def test_clear_flag_controls_wipe_query(tmp_path: pathlib.Path) -> None:
    pkg_dir = tmp_path / "presets" / "fine_format"
    _write_pyproject(pkg_dir, "fine_format")
    projects = [WorkspaceProject(path=pkg_dir, config_status=ProjectConfigStatus.VALID)]
    results: dict[pathlib.Path, CollectProjectDependencyInfoRunResult] = {
        pkg_dir: CollectProjectDependencyInfoRunResult(
            package_name="fine_format", package_kind="preset"
        )
    }

    graph_wipe = _RecordingGraph()
    _ = _run(
        _make_handler(projects, results, graph_wipe),
        SeedWorkspaceDependencyGraphRunPayload(clear_existing=True),
    )
    assert graph_wipe.calls[0][0] == "MATCH (n) DETACH DELETE n"

    graph_keep = _RecordingGraph()
    _ = _run(
        _make_handler(projects, results, graph_keep),
        SeedWorkspaceDependencyGraphRunPayload(clear_existing=False),
    )
    assert all(
        not query.startswith("MATCH (n) DETACH DELETE") for query, _ in graph_keep.calls
    )


def test_edge_query_shapes_are_unchanged(tmp_path: pathlib.Path) -> None:
    consumer_dir = tmp_path / "consumer"
    _write_pyproject(consumer_dir, "consumer")
    dep_dir = tmp_path / "presets" / "fine_format"
    _write_pyproject(dep_dir, "fine_format")
    projects = [
        WorkspaceProject(path=consumer_dir, config_status=ProjectConfigStatus.VALID),
        WorkspaceProject(path=dep_dir, config_status=ProjectConfigStatus.VALID),
    ]
    results: dict[pathlib.Path, CollectProjectDependencyInfoRunResult] = {
        consumer_dir: CollectProjectDependencyInfoRunResult(
            package_name="consumer",
            package_kind="core",
            pyproject_dependencies=["fine_format"],
            used_preset_sources=["fine_format"],
        ),
        dep_dir: CollectProjectDependencyInfoRunResult(
            package_name="fine_format", package_kind="preset"
        ),
    }
    graph = _RecordingGraph()
    handler = _make_handler(projects, results, graph)

    result = _run(handler, SeedWorkspaceDependencyGraphRunPayload(clear_existing=False))

    assert result.depends_on_edges == 1
    assert result.uses_preset_edges == 1
    edge_calls = [(query, params) for query, params in graph.calls if "CREATE" in query]
    assert len(edge_calls) == 2
    assert edge_calls[0][0] == (
        "MATCH (a:Package {name: $src}), (b:Package {name: $dst}) "
        "CREATE (a)-[:DEPENDS_ON {source_file: $src_file}]->(b)"
    )
    assert edge_calls[1][0] == (
        "MATCH (a:Package {name: $src}), (b:Package {name: $dst}) "
        "CREATE (a)-[:USES_PRESET {source_file: $src_file}]->(b)"
    )
    assert edge_calls[0][1]["src_file"].endswith("pyproject.toml")


def test_collecting_handler_reports_legacy_preset_kind(
    tmp_path: pathlib.Path,
) -> None:
    pkg_dir = tmp_path / "extensions" / "fine_format"
    _write_pyproject(pkg_dir, "fine_format")
    project_info_provider = mock.create_autospec(
        iprojectinfoprovider.IProjectInfoProvider, instance=True
    )
    project_info_provider.get_current_project_dir_path.return_value = pkg_dir
    handler = PyprojectPackageInfoHandler(project_info_provider=project_info_provider)
    run_context = CollectProjectDependencyInfoRunContext(
        run_id=1,
        initial_payload=CollectProjectDependencyInfoRunPayload(),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=code_action.RunContextInfoProvider(is_concurrent_execution=True),
    )

    result = asyncio.run(
        handler.run(CollectProjectDependencyInfoRunPayload(), run_context)
    )

    assert result.package_name == "fine_format"
    assert result.package_kind == "preset"
