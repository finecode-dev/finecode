"""WM-side project env-universe lookup and interpreter-selection resolution
for run entry points (PRD-0003 AC8).

Wraps the pure ``env_selection`` resolver with the WM's raw-config lookup, so
run entry points (CLI `run`, IDE streaming, non-streaming) can resolve
`--env`/`--interpreter` selectors (and the config `default_interpreters`
default) into a concrete set of selected interpreter canonicals to pass to
the matrix fan-out sites (`matrix_runner`, `matrix_streaming`) — mirroring
`prepare_envs_service`'s use of the same pure resolver for `prepare-envs`.
"""
from __future__ import annotations

import pathlib
import typing

from finecode.wm_server import context
from finecode.wm_server.config import env_selection
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

__all__ = [
    "project_env_universe_from_raw",
    "selected_interpreters_for_project",
    "validate_run_selectors",
]


def project_env_universe_from_raw(raw_config: dict[str, typing.Any]) -> dict[str, typing.Any]:
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


def selected_interpreters_for_project(
    project_path: pathlib.Path,
    env_selectors: list[str],
    interpreter_selectors: list[str],
    dev_env: str,
    ws_context: context.WorkspaceContext,
) -> set[str] | None:
    """Resolve `--env`/`--interpreter` selectors (+ config default) for one
    project's matrix envs into a set of selected interpreter canonicals, or
    ``None`` when nothing narrows the axis (callers then run the full axis).

    Raises:
        env_selection.EnvSelectionError: A config default or explicit
            selector names an interpreter not in its base's declared axis.
    """
    raw_config = ws_context.ws_projects_raw_configs.get(project_path, {})
    env_universe = project_env_universe_from_raw(raw_config)
    return env_selection.resolve_selected_interpreters(
        env_universe, env_selectors, interpreter_selectors, dev_env
    )


def validate_run_selectors(
    env_selectors: list[str],
    interpreter_selectors: list[str],
    project_paths: list[pathlib.Path],
    ws_context: context.WorkspaceContext,
) -> None:
    """Raise ``ActionRunFailed`` if an explicit ``--env``/``--interpreter``
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
    for selector in env_selectors:
        if not any(env_selection.env_selector_known_in(selector, u) for u in universes):
            raise ActionRunFailed(f"Unknown environment: '{selector}'")
    for selector in interpreter_selectors:
        if not any(
            env_selection.interpreter_selector_known_in(selector, u) for u in universes
        ):
            raise ActionRunFailed(f"Unknown interpreter: '{selector}'")
