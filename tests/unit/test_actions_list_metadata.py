from __future__ import annotations

import pathlib
from unittest import mock

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers import _workspace
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import project_resolution_service, run_service
from finecode.wm_server.services.run_service import proxy_utils


def _make_project(dir_path: pathlib.Path) -> domain.ResolvedProject:
    def _action(name: str, env: str) -> domain.Action:
        return domain.Action(
            name=name,
            source=f"pkg.{name.capitalize()}Action",
            handlers=[
                domain.ActionHandler(
                    name=f"{name}_h",
                    source=f"pkg.{name}Handler",
                    config={},
                    env=env,
                    dependencies=[],
                )
            ],
            config={},
        )

    collected = domain.CollectedProject(
        name="p",
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[_action("a", "env_a"), _action("b", "env_b")],
        services=[],
        action_handler_configs={},
    )
    return domain.ResolvedProject.from_collected(collected)


def _mock_resolution(project: domain.ResolvedProject, monkeypatch) -> None:
    class _Outcome:
        def require(self, paths):
            return [project]

    async def _resolved(paths, ws_context):
        return _Outcome()

    monkeypatch.setattr(
        project_resolution_service, "ensure_projects_resolved", _resolved
    )


async def test_only_filtered_venv_is_dumped(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """With names=["a"], only a's venv is dumped and nothing ever starts."""
    project = _make_project(tmp_path)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = project
    _mock_resolution(project, monkeypatch)
    seen: list = []

    async def _resolving(project_arg, ws_context_arg, **kwargs):
        seen.append([a.name for a in kwargs.get("actions", [])])
        for action in kwargs.get("actions", []):
            action.canonical_source = "resolved." + action.source
        return {}

    with (
        mock.patch.object(
            run_service, "resolve_unresolved_metadata", side_effect=_resolving
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
        result = await _workspace._handle_list_actions(
            {"project": str(tmp_path), "names": ["a"]}, ws_context
        )

    assert seen == [["a"]]
    assert [a["name"] for a in result["actions"]] == ["a"]
    assert result["actions"][0]["canonicalSource"] is not None


async def test_still_unresolved_target_listed_null(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """A target nothing resolves is still listed with a null canonical, not an error."""
    project = _make_project(tmp_path)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = project
    _mock_resolution(project, monkeypatch)

    async def _empty(project_arg, ws_context_arg, **kwargs):
        return {}

    with (
        mock.patch.object(
            run_service, "resolve_unresolved_metadata", side_effect=_empty
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
        result = await _workspace._handle_list_actions(
            {"project": str(tmp_path), "names": ["a"]}, ws_context
        )

    assert [a["name"] for a in result["actions"]] == ["a"]
    assert result["actions"][0]["canonicalSource"] is None
