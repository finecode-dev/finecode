"""WM-side project env-universe lookup and interpreter-selection resolution
for run entry points (PRD-0003 AC8, ADR-0103).

Wraps the pure ``env_selection`` resolver with the WM's raw-config lookup, so
run entry points (CLI `run`, IDE streaming, non-streaming) can resolve
`--interpreter` selectors (and the config `default_interpreters`
default) into a concrete set of selected env names to pass to
the matrix fan-out sites.
"""

from __future__ import annotations

import pathlib
import typing

from finecode import user_messages
from finecode.wm_server import context, domain
from finecode.wm_server.config import env_selection
from finecode.wm_server.services.run_service import matrix_runner
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

__all__ = [
    "check_variant_selection",
    "project_env_universe_from_raw",
    "selected_envs_for_project",
    "selection_for_matrixed_actions",
    "validate_run_selectors",
]


def project_env_universe_from_raw(
    raw_config: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    """The project's full env-name -> `tool.finecode.env` entry map.

    Merges in `dependency-groups` names so envs that have no
    `tool.finecode.env` entry at all (the common case for plain,
    unconfigured envs — e.g. this repo's own `dev`/`dev_workspace`) are
    still part of the selection universe, instead of being silently
    excluded once a selection is active elsewhere in the project. Matrix
    children always have a `tool.finecode.env` entry (materialized by
    `read_configs.resolve_interpreter_matrices`), so they are covered
    either way.
    """
    finecode_section = raw_config.get("tool", {}).get("finecode", {})
    env_table: dict[str, typing.Any] = finecode_section.get("env", {})
    deps_groups: dict[str, typing.Any] = raw_config.get("dependency-groups", {})
    universe = {name: env_table.get(name, {}) for name in deps_groups}
    for name, entry in env_table.items():
        universe.setdefault(name, entry)
    return universe


def selected_envs_for_project(
    project_path: pathlib.Path,
    interpreter_selectors: list[str],
    dev_env: str,
    ws_context: context.WorkspaceContext,
) -> set[str] | None:
    """Resolve `--interpreter` selectors (+ config default) for one
    project's matrix envs into selected concrete env names, or
    ``None`` when every base's effective set equals its full axis (callers
    then run the full axis).

    Raises:
        env_selection.EnvSelectionError: A config default names an interpreter
            not in its base's declared axis.
    """
    raw_config = ws_context.ws_projects_raw_configs.get(project_path, {})
    env_universe = project_env_universe_from_raw(raw_config)
    return env_selection.resolve_run_selection(
        env_universe, interpreter_selectors, dev_env
    )


def selection_for_matrixed_actions(
    actions_by_project: dict[pathlib.Path, list[str]],
    interpreter_selectors: list[str],
    dev_env: str,
    ws_context: context.WorkspaceContext,
) -> dict[pathlib.Path, set[str] | None]:
    """Per-project env selection, computed only where a listed action
    is matrixed (``matrix_runner.is_matrixed`` on the collected project).
    Projects without a matrixed action are left out of the dict.

    Computing eagerly for every project would add a new failure to
    non-matrixed runs: ``resolve_env_selection`` validates every declared
    policy and raises ``EnvSelectionError``. Mirrors the dispatch, which
    only computes the selection inside ``if is_matrixed``.

    Raises:
        env_selection.EnvSelectionError: same as
            ``selected_envs_for_project``.
    """
    selection: dict[pathlib.Path, set[str] | None] = {}
    for project_path, action_names in actions_by_project.items():
        project = ws_context.ws_projects.get(project_path)
        if not isinstance(project, domain.CollectedProject):
            continue
        if not any(
            matrix_runner.is_matrixed(action)
            for action in project.actions
            if action.name in action_names
        ):
            continue
        selection[project_path] = selected_envs_for_project(
            project_path, interpreter_selectors, dev_env, ws_context
        )
    return selection


def validate_run_selectors(
    interpreter_selectors: list[str],
    project_paths: list[pathlib.Path],
    ws_context: context.WorkspaceContext,
) -> None:
    """Raise ``ActionRunFailed`` if an explicit ``--interpreter``
    selector matches no in-scope project's matrix envs.

    Mirrors ``prepare_envs_service``'s cross-project validation: a selector
    that matches at least one of *project_paths* is tolerated (a selector
    valid for one project but absent in another must not fail the whole
    run) — only a selector unknown to *every* in-scope project is an error.
    """
    if not project_paths:
        return

    universes = [
        project_env_universe_from_raw(ws_context.ws_projects_raw_configs.get(p, {}))
        for p in project_paths
    ]
    for selector in interpreter_selectors:
        if not any(
            env_selection.interpreter_selector_known_in(selector, u) for u in universes
        ):
            raise ActionRunFailed(f"Unknown interpreter: '{selector}'")


async def check_variant_selection(
    actions_by_project: dict[pathlib.Path, list[str]],
    selected_envs_by_project: dict[pathlib.Path, set[str] | None],
    interpreter_selectors: list[str],
    ws_context: context.WorkspaceContext,
) -> None:
    """Warn or fail when the selection yields no variant for matrixed actions."""
    pairs: list[tuple[domain.CollectedProject, domain.Action]] = []
    for project_path, action_names in actions_by_project.items():
        selection = selected_envs_by_project.get(project_path)
        if selection is None:
            continue
        project = ws_context.ws_projects.get(project_path)
        if not isinstance(project, domain.CollectedProject):
            continue
        pairs.extend(
            (project, action)
            for action in project.actions
            if action.name in action_names and matrix_runner.is_matrixed(action)
        )
    empty = [
        (project, action)
        for project, action in pairs
        if not matrix_runner.selected_variants(
            action, selected_envs_by_project.get(project.dir_path)
        )
    ]
    if not pairs:
        return
    if interpreter_selectors:
        why = f"--interpreter={','.join(interpreter_selectors)}"
    else:
        why = "the default_interpreters policy"
    if len(empty) == len(pairs):
        raise ActionRunFailed(
            f"No interpreter variant selected: {why} matches no interpreter of "
            f"{sorted({a.name for _, a in empty})} in "
            f"{sorted({p.name or str(p.dir_path) for p, _ in empty})}"
        )
    if empty:
        await user_messages.warning(
            f"No interpreter variant selected for {len(empty)} matrixed action run(s) "
            f"({why}); nothing runs for: "
            f"{', '.join(f'{a.name} in {p.name or p.dir_path}' for p, a in empty)}"
        )
