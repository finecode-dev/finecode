"""Root-first resolution and the run-entry gates (C, D).

A run whose every action is workspace-scoped and hosted on the workspace root
resolves the root only; anything else resolves every project, failing loudly on
a failed sibling instead of skipping it.  The run-entry helpers
(``_parse_and_validate_run_action_params``, ``_resolve_source_to_name``,
``run_action_with_partial_results``) gate through this service, and a project
rebuilt by the gate keeps handler-config overrides applied at the owner.
"""

from __future__ import annotations

import contextlib
import pathlib
import typing

import pytest
from resolution_fake import SwappingResolver, make_preset_action

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._actions import _handle_set_config_overrides
from finecode.wm_server._api_handlers._helpers import (
    _parse_and_validate_run_action_params,
)
from finecode.wm_server._api_handlers._streaming import _resolve_source_to_name
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import partial_results_service, run_service
from finecode.wm_server.services.run_service import (
    DevEnv,
    RunActionTrigger,
    proxy_utils,
)
from finecode.wm_server.testing import make_running_runner

_LINT_HANDLER_SOURCE = "test.handlers.RuffHandler"
_LINT_ACTION_SOURCE = "test.actions.LintAction"


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


# --------------------------------------------------------------------------- #
# run_action_with_partial_results helpers
# --------------------------------------------------------------------------- #


class _EmptyStream:
    progress = None
    responses: list = []

    def __iter__(self) -> typing.Iterator[object]:
        return iter(())

    def __aiter__(self) -> "_EmptyStreamIterator":
        return _EmptyStreamIterator()


class _EmptyStreamIterator:
    def __aiter__(self) -> "_EmptyStreamIterator":
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


@contextlib.asynccontextmanager
async def _noop_run_with_partial_results(**kwargs: object):
    yield _EmptyStream()


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        partial_results_service,
        "run_with_partial_results",
        _noop_run_with_partial_results,
    )


async def _run_partial(
    ws_context: context.WorkspaceContext,
    action_name: str,
    *,
    project_path: str = "",
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict]:
    started: list[dict] = []

    async def _recording_start(
        actions_by_projects: dict,
        ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        started.append(actions_by_projects)

    monkeypatch.setattr(
        partial_results_service, "start_required_environments", _recording_start
    )
    _stub_dispatch(monkeypatch)
    await partial_results_service.run_action_with_partial_results(
        action_name=action_name,
        project_path=project_path,
        params={},
        partial_result_token="tok",
        run_trigger=RunActionTrigger("system"),
        dev_env=DevEnv("ide"),
        ws_context=ws_context,
        origin=None,
        result_formats=["json"],
    )
    return started


# --------------------------------------------------------------------------- #
# the gates
# --------------------------------------------------------------------------- #


async def test_parse_explicit_project_finds_the_preset_action(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``actions/run --project`` on a lazily attached WM resolves the named
    project and finds its preset-contributed action — a stale pre-resolution
    reference would miss it (AC6a)."""
    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_collected(a)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="lint",
                source=_LINT_ACTION_SOURCE,
                scope=domain.ActionScope.PROJECT,
            )
        ],
    )

    parsed = await _parse_and_validate_run_action_params(
        {"actionSource": _LINT_ACTION_SOURCE, "project": str(a)}, ws_context
    )

    assert resolver.calls == [[a]]
    assert parsed.action.name == "lint"
    assert isinstance(parsed.project, domain.ResolvedProject)


async def test_parse_ensures_metadata_before_the_scope_check(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly rebuilt project's action has no metadata; the explicit-project
    path resolves it before trusting the (still-unknown) scope."""
    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_collected(a)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="lint",
                source=_LINT_ACTION_SOURCE,
                scope=domain.ActionScope.PROJECT,
                no_metadata=True,
            )
        ],
    )
    ensured: list[tuple[str, pathlib.Path]] = []

    async def _setting_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        ensured.append((action.name, project.dir_path))
        action.scope = domain.ActionScope.PROJECT
        action.canonical_source = _LINT_ACTION_SOURCE

    monkeypatch.setattr(run_service, "ensure_action_metadata", _setting_ensure_metadata)

    parsed = await _parse_and_validate_run_action_params(
        {"actionSource": _LINT_ACTION_SOURCE, "project": str(a)}, ws_context
    )

    assert resolver.calls == [[a]]
    assert ensured == [("lint", a)]
    assert parsed.action.scope == domain.ActionScope.PROJECT


async def test_streamed_resolve_source_to_name_is_root_first(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streamed ``actions/run`` (``project=""``) path resolves the root for
    a workspace-scoped root-hosted action and stops there (AC6c)."""
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

    name = await _resolve_source_to_name(
        "fine_inspect_code.InspectCodeAction", "", ws_context
    )

    assert name == "inspect_code"
    assert resolver.calls == [[root]]


async def test_streamed_run_of_workspace_action_targets_only_the_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_action_with_partial_results("", project_path="")`` for a
    workspace-scoped root-hosted action dispatches to the fresh root alone
    (AC6c)."""
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

    started = await _run_partial(ws_context, "inspect_code", monkeypatch=monkeypatch)

    assert resolver.calls == [[root]]
    assert started == [{root: ["inspect_code"]}]


async def test_streamed_run_fails_loudly_on_a_failed_sibling(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project-scoped run with one failing sibling raises, naming it, instead
    of running partially over the healthy ones."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        preset_actions=[
            make_preset_action(
                name="lint",
                source=_LINT_ACTION_SOURCE,
                scope=domain.ActionScope.PROJECT,
            )
        ],
        fail_paths={sibling},
        fail_message="venv missing",
    )

    with pytest.raises(run_service.ActionRunFailed) as excinfo:
        await _run_partial(ws_context, "lint", monkeypatch=monkeypatch)

    assert resolver.calls == [[root], [sibling]]
    assert "venv missing" in excinfo.value.message


async def test_sibling_only_action_reaches_dispatch_instead_of_scope_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project-scoped action present only in a sibling — whose fresh object
    has no metadata — gets its metadata ensured and dispatches to the sibling
    instead of raising ``ActionNotResolvableError`` (AC16)."""
    root, sibling, ws_context = _make_root_and_sibling(tmp_path)
    # The sibling's own action lacks metadata, exactly like a freshly rebuilt
    # project that had no root metadata to propagate from.
    supplied = make_preset_action(
        name="special_tool",
        source="test.actions.SpecialToolAction",
        scope=domain.ActionScope.PROJECT,
        no_metadata=True,
    )
    sibling_project = _make_collected(sibling)
    sibling_project.actions.append(supplied)
    ws_context.ws_projects[sibling] = sibling_project
    resolver = SwappingResolver(ws_context, monkeypatch)

    ensured: list[pathlib.Path] = []

    async def _setting_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        ensured.append(project.dir_path)
        action.canonical_source = "test.actions.SpecialToolAction"
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(proxy_utils, "ensure_action_metadata", _setting_ensure_metadata)

    started = await _run_partial(ws_context, "special_tool", monkeypatch=monkeypatch)

    assert ensured == [sibling]
    assert resolver.calls == [[root], [sibling]]
    assert started == [{sibling: ["special_tool"]}]


# --------------------------------------------------------------------------- #
# AC2 through the gates: overrides survive the gate's rebuild
# --------------------------------------------------------------------------- #


def _build_raw_config() -> dict:
    return {
        "tool": {
            "finecode": {
                "env": {"dev_workspace": {}},
                "action": {
                    "lint": {
                        "source": _LINT_ACTION_SOURCE,
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


def _make_lint_collected(path: pathlib.Path) -> domain.CollectedProject:
    handler = domain.ActionHandler(
        name="ruff",
        source=_LINT_HANDLER_SOURCE,
        config={},
        env="dev_workspace",
        dependencies=[],
    )
    action = domain.Action(
        name="lint",
        source=_LINT_ACTION_SOURCE,
        handlers=[handler],
        config={},
    )
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
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


def _real_start_stubs(
    monkeypatch: pytest.MonkeyPatch,
    ws_context: context.WorkspaceContext,
) -> list[tuple[pathlib.Path, dict]]:
    recorded: list[tuple[pathlib.Path, dict]] = []

    async def _fake_start_dev_workspace_runner(
        project_def: domain.Project,
        ws_context: context.WorkspaceContext,
        **kwargs: object,
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
    return recorded


async def test_override_survives_the_gate_rebuild(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``finecode run --config ...`` override set before the run survives the
    project being rebuilt by the resolution gate — the rebuild applies the
    stored overrides at the owner (AC2)."""
    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_lint_collected(a)
    ws_context.ws_projects_raw_configs[a] = _build_raw_config()
    await _handle_set_config_overrides(
        {"overrides": {"lint": {"": {"line_length": "120"}}}, "serviceOverrides": {}},
        ws_context,
    )

    recorded = _real_start_stubs(monkeypatch, ws_context)

    async def _setting_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        action.canonical_source = _LINT_ACTION_SOURCE
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(run_service, "ensure_action_metadata", _setting_ensure_metadata)

    parsed = await _parse_and_validate_run_action_params(
        {"actionSource": _LINT_ACTION_SOURCE, "project": str(a)}, ws_context
    )
    assert parsed.action.name == "lint"
    assert recorded == [(a, {_LINT_HANDLER_SOURCE: {"line_length": "120"}})]


async def test_override_survives_the_no_project_gate_shape(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-``--project`` shape (``runBatch`` auto-discovery) rebuilds the
    root through the gate and the rebuilt project still carries the override
    (AC2)."""
    from finecode.wm_server._api_handlers._helpers import _resolve_actions_by_project

    a = tmp_path / "a"
    ws_context = context.WorkspaceContext(ws_dirs_paths=[a])
    ws_context.ws_projects[a] = _make_lint_collected(a)
    ws_context.ws_projects_raw_configs[a] = _build_raw_config()
    await _handle_set_config_overrides(
        {"overrides": {"lint": {"": {"line_length": "120"}}}, "serviceOverrides": {}},
        ws_context,
    )

    recorded = _real_start_stubs(monkeypatch, ws_context)

    async def _setting_ensure_metadata(
        action: domain.Action,
        project: domain.CollectedProject,
        ws_context: context.WorkspaceContext,
    ) -> None:
        action.canonical_source = _LINT_ACTION_SOURCE
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(proxy_utils, "ensure_action_metadata", _setting_ensure_metadata)

    actions_by_project, _ = await _resolve_actions_by_project(
        None, [_LINT_ACTION_SOURCE], ws_context
    )

    assert actions_by_project == {a: ["lint"]}
    assert recorded == [(a, {_LINT_HANDLER_SOURCE: {"line_length": "120"}})]
