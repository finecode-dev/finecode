from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client, runner_manager
from finecode.wm_server.testing import make_running_runner

_SOURCE = "test.actions.CachedAction"
_CANONICAL = "test.actions.impl.CachedAction"
_PARENT = "test.actions.Parent"
_LANGUAGE = "python"
_FILE_LOC = "test/actions/impl.py:10"


def _make_project(
    tmp_path: pathlib.Path, *, canonical: str | None = None
) -> domain.CollectedProject:
    action = domain.Action(
        name="cached",
        source=_SOURCE,
        handlers=[
            domain.ActionHandler(
                name="h",
                source="test.handlers.H",
                config={},
                env="dev_no_runtime",
                dependencies=[],
            )
        ],
        config={},
    )
    if canonical is not None:
        action.canonical_source = canonical
        action.parent_action_source = _PARENT
        action.language = _LANGUAGE
        action.file_loc = _FILE_LOC
    else:
        action.canonical_source = _CANONICAL
        action.parent_action_source = _PARENT
        action.language = _LANGUAGE
        action.file_loc = _FILE_LOC
        action.meta_from_cache = True
    return domain.CollectedProject(
        name="p",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[action],
        services=[],
        action_handler_configs={},
    )


def _meta(**overrides) -> dict:
    base = {
        "canonical_source": _CANONICAL,
        "runs_concurrently": False,
        "scope": "project",
        "parentActionSource": _PARENT,
        "language": _LANGUAGE,
        "fileLoc": _FILE_LOC,
    }
    base.update(overrides)
    return base


async def _report(
    tmp_path: pathlib.Path,
    project: domain.CollectedProject,
    meta: dict,
    env_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[domain.Action, list[str]]:
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = project
    runner = make_running_runner(working_dir_path=tmp_path, env_name=env_name)

    async def _noop_update(*args, **kwargs) -> None:
        return None

    async def _meta_response(*args, **kwargs) -> dict:
        return {"actions": {_SOURCE: meta}, "handlers": {}}

    warnings: list[str] = []
    monkeypatch.setattr(runner_client, "update_config", _noop_update)
    monkeypatch.setattr(runner_client, "resolve_action_meta", _meta_response)
    monkeypatch.setattr(
        runner_manager.logger,
        "warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )
    await runner_manager.update_runner_config(runner, project, None, ws_context)
    return project.actions[0], warnings


async def test_equal_report_clears_flag_silently(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ER confirming the cached value clears it without warning, so steady state stays quiet."""
    project = _make_project(tmp_path)

    action, warnings = await _report(
        tmp_path, project, _meta(), "dev_no_runtime", monkeypatch
    )

    assert action.meta_from_cache is False
    assert action.canonical_source == _CANONICAL
    assert warnings == []


async def test_file_loc_only_difference_is_silent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relocated file is still the same action, so only the location is updated."""
    project = _make_project(tmp_path)

    action, warnings = await _report(
        tmp_path,
        project,
        _meta(fileLoc="elsewhere.py:99"),
        "dev_no_runtime",
        monkeypatch,
    )

    assert action.meta_from_cache is False
    assert action.file_loc == "elsewhere.py:99"
    assert warnings == []


async def test_identity_difference_warns_and_overwrites(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler-env ER disagreeing on identity replaces the cache and names both values."""
    project = _make_project(tmp_path)

    action, warnings = await _report(
        tmp_path,
        project,
        _meta(canonical_source="other.Canonical", parentActionSource="other.Parent"),
        "dev_no_runtime",
        monkeypatch,
    )

    assert action.meta_from_cache is False
    assert action.canonical_source == "other.Canonical"
    assert action.parent_action_source == "other.Parent"
    assert len(warnings) == 1
    assert _CANONICAL in warnings[0]
    assert "other.Canonical" in warnings[0]


async def test_report_from_another_env_keeps_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hosting env never overrules the cache, so wheel and editable reports do not flap."""
    project = _make_project(tmp_path)

    action, warnings = await _report(
        tmp_path,
        project,
        _meta(canonical_source="other.Canonical"),
        "testing@cpython-3.14",
        monkeypatch,
    )

    assert action.meta_from_cache is True
    assert action.canonical_source == _CANONICAL
    assert warnings == []


async def test_er_report_first_then_cache_apply_is_a_noop(
    tmp_path: pathlib.Path,
) -> None:
    """Once an ER has resolved an action, the opportunistic cache has nothing to fill."""
    action_meta_cache = pytest.importorskip(
        "finecode.wm_server.services.action_meta_cache"
    )
    project = _make_project(tmp_path, canonical=_CANONICAL)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    applied = await action_meta_cache.resolve_unresolved(project, ws_context)
    assert applied == {}


def test_propagation_carries_the_cache_flag(tmp_path: pathlib.Path) -> None:
    """Siblings filled from a cached action stay correctable until their own env reports."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    source_dir = tmp_path / "src"
    sibling_dir = tmp_path / "sibling"
    source_project = domain.CollectedProject(
        name="src",
        dir_path=source_dir,
        def_path=source_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )
    resolved = domain.Action(
        name="a",
        source=_SOURCE,
        handlers=[
            domain.ActionHandler(
                name="h",
                source="test.H",
                config={},
                env="dev_no_runtime",
                dependencies=[],
            )
        ],
        config={},
    )
    resolved.canonical_source = _CANONICAL
    resolved.meta_from_cache = True
    source_project.actions.append(resolved)
    sibling = domain.Action(
        name="a",
        source=_SOURCE,
        handlers=[
            domain.ActionHandler(
                name="h",
                source="test.H",
                config={},
                env="dev_no_runtime",
                dependencies=[],
            )
        ],
        config={},
    )
    sibling_project = domain.CollectedProject(
        name="sib",
        dir_path=sibling_dir,
        def_path=sibling_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[sibling],
        services=[],
        action_handler_configs={},
    )
    ws_context.ws_projects[source_dir] = source_project
    ws_context.ws_projects[sibling_dir] = sibling_project

    runner_manager.propagate_action_meta(resolved, source_project, ws_context)

    assert sibling.canonical_source == _CANONICAL
    assert sibling.meta_from_cache is True

    resolved.meta_from_cache = False
    sibling.canonical_source = None
    sibling.meta_from_cache = False

    runner_manager.propagate_action_meta(resolved, source_project, ws_context)

    assert sibling.canonical_source == _CANONICAL
    assert sibling.meta_from_cache is False
