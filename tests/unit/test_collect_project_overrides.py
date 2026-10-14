"""Handler-config overrides survive a project being rebuilt from raw config.

Config overrides are applied at the single site that builds ``action_handler_configs``
from a raw config — ``collect_project`` — so any later rebuild (resolution, runner
start, ``setConfigOverrides``) carries them.  Without this, a ``finecode run --config
lint.ruff.line_length=120 lint`` run would configure the runner with the project's
stored config on the first pass and then silently drop the override when the project
is rebuilt from the preset-merged config, which is exactly the ``--project X --config
...`` bug on HEAD.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._actions import _handle_set_config_overrides
from finecode.wm_server.config import collect_actions
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.testing import make_running_runner

_LINT_HANDLER_SOURCE = "fine_python_ruff.RuffHandler"
_OVERRIDE = {"lint": {"": {"line_length": "120"}}}


def _build_raw_config() -> dict[str, Any]:
    return {
        "tool": {
            "finecode": {
                "env": {"dev_workspace": {}},
                "action": {
                    "lint": {
                        "source": "fine_lint.LintAction",
                        "handlers": [
                            {
                                "name": "ruff",
                                "source": _LINT_HANDLER_SOURCE,
                                "env": "dev_workspace",
                            }
                        ],
                    }
                },
                "action_handler": [
                    {
                        "source": _LINT_HANDLER_SOURCE,
                        "config": {"line_length": "88"},
                    }
                ],
            }
        }
    }


def _make_lint_project(dir_path: Path) -> domain.CollectedProject:
    handler = domain.ActionHandler(
        name="ruff",
        source=_LINT_HANDLER_SOURCE,
        config={},
        env="dev_workspace",
        dependencies=[],
    )
    action = domain.Action(
        name="lint",
        source="fine_lint.LintAction",
        handlers=[handler],
        config={},
    )
    return domain.CollectedProject(
        name="proj",
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={
            "dev_workspace": domain.EnvConfig(
                runner_config=domain.RunnerConfig(debug=False)
            )
        },
        actions=[action],
        services=[],
        action_handler_configs={},
    )


def _make_context(dir_path: Path) -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext(ws_dirs_paths=[dir_path])
    ws_context.ws_projects[dir_path] = _make_lint_project(dir_path)
    ws_context.ws_projects_raw_configs[dir_path] = _build_raw_config()
    return ws_context


async def test_collect_project_reapplies_stored_overrides(
    tmp_path: Path,
) -> None:
    """A project rebuilt from raw config keeps the stored overrides, and so
    does a second rebuild — one rebuild must not clear them for the next."""
    ws_context = _make_context(tmp_path)
    ws_context.handler_config_overrides = _OVERRIDE

    collect_actions.collect_project(project_path=tmp_path, ws_context=ws_context)
    project = ws_context.ws_projects[tmp_path]
    assert project.action_handler_configs[_LINT_HANDLER_SOURCE]["line_length"] == "120"

    collect_actions.collect_project(project_path=tmp_path, ws_context=ws_context)
    assert (
        ws_context.ws_projects[tmp_path].action_handler_configs[_LINT_HANDLER_SOURCE][
            "line_length"
        ]
        == "120"
    )


async def _record_rebuild(
    ws_context: context.WorkspaceContext,
    projects: list[domain.Project],
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Path, dict[str, dict[str, Any]]]]:
    recorded: list[tuple[Path, dict[str, dict[str, Any]]]] = []

    async def _fake_start_dev_workspace_runner(
        project_def: domain.Project,
        ws_context: context.WorkspaceContext,
        *,
        cmd_override: str | None = None,
    ) -> object:
        runner = make_running_runner(
            working_dir_path=project_def.dir_path, env_name="dev_workspace"
        )
        ws_context.ws_projects_extension_runners.setdefault(project_def.dir_path, {})[
            "dev_workspace"
        ] = runner
        return runner

    async def _stub_read_project_config(*args: object, **kwargs: object) -> None:
        pass

    async def _recording_update_runner_config(
        *,
        runner: object,
        project: domain.CollectedProject,
        handlers_to_initialize: object,
        ws_context: context.WorkspaceContext,
        pass_label: str = "other",
    ) -> None:
        recorded.append((project.dir_path, dict(project.action_handler_configs)))

    monkeypatch.setattr(
        runner_manager, "_start_dev_workspace_runner", _fake_start_dev_workspace_runner
    )
    monkeypatch.setattr(
        runner_manager.preset_resolution,
        "read_project_config_with_py_presets",
        _stub_read_project_config,
    )
    monkeypatch.setattr(
        runner_manager, "update_runner_config", _recording_update_runner_config
    )

    await runner_manager.start_runners_with_presets(projects, ws_context)
    return recorded


async def test_single_project_rebuild_carries_override_to_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner configured from a rebuilt project receives the override.

    The ``--project X --config ...`` shape: ``setConfigOverrides`` stores the
    override, then resolving X rebuilds it from the (now preset-merged) raw
    config inside ``start_runners_with_presets``; the rebuilt project handed to
    ``update_runner_config`` must carry the override, the raw value replaced.
    On HEAD, ``collect_project`` builds fresh ``action_handler_configs`` from the
    raw config and the override is lost.
    """
    ws_context = _make_context(tmp_path)
    await _handle_set_config_overrides(
        {"overrides": _OVERRIDE, "serviceOverrides": {}}, ws_context
    )

    recorded = await _record_rebuild(
        ws_context, [ws_context.ws_projects[tmp_path]], monkeypatch
    )

    assert recorded == [(tmp_path, {_LINT_HANDLER_SOURCE: {"line_length": "120"}})]


async def test_multi_project_rebuild_carries_override_to_every_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every project rebuilt in one batch carries the override.

    The no-``--project`` shape resolves every project in one
    ``start_runners_with_presets`` call; each rebuilt project's runner config
    must carry the override.
    """
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"
    ws_context = _make_context(path_a)
    ws_context.ws_projects[path_b] = _make_lint_project(path_b)
    ws_context.ws_projects_raw_configs[path_b] = _build_raw_config()

    await _handle_set_config_overrides(
        {"overrides": _OVERRIDE, "serviceOverrides": {}}, ws_context
    )

    recorded = await _record_rebuild(
        ws_context,
        [ws_context.ws_projects[path_a], ws_context.ws_projects[path_b]],
        monkeypatch,
    )

    assert recorded == [
        (path_a, {_LINT_HANDLER_SOURCE: {"line_length": "120"}}),
        (path_b, {_LINT_HANDLER_SOURCE: {"line_length": "120"}}),
    ]


async def test_set_config_overrides_replaces_not_merges(
    tmp_path: Path,
) -> None:
    """A second ``setConfigOverrides`` replaces the first, on rebuilt and
    untouched projects alike.  Without replace semantics, a rebuilt project
    would keep the first override while a never-rebuilt one flipped to the
    second, so a later caller could not rely on the current value."""
    unresolved = _make_lint_project(tmp_path / "unresolved")
    resolved = domain.ResolvedProject.from_collected(
        _make_lint_project(tmp_path / "resolved")
    )
    ws_context = context.WorkspaceContext(
        ws_dirs_paths=[tmp_path / "unresolved", tmp_path / "resolved"]
    )
    for project in (unresolved, resolved):
        ws_context.ws_projects[project.dir_path] = project
        ws_context.ws_projects_raw_configs[project.dir_path] = _build_raw_config()

    await _handle_set_config_overrides(
        {"overrides": {"lint": {"": {"a": "1"}}}, "serviceOverrides": {}},
        ws_context,
    )
    await _handle_set_config_overrides(
        {"overrides": {"lint": {"": {"b": "2"}}}, "serviceOverrides": {}},
        ws_context,
    )

    for project in ws_context.ws_projects.values():
        config = project.action_handler_configs[_LINT_HANDLER_SOURCE]
        assert config.get("b") == "2"
        assert "a" not in config
