"""Action lookup by import-path alias across a project's action set (ADR-0019)."""

from __future__ import annotations

from loguru import logger

from finecode.wm_server import context, domain

__all__ = ["find_action_by_source", "project_exposes_action"]


def _matches_source(action: domain.Action, source: str) -> bool:
    # canonical_source is set by update_runner_config for every action whose class
    # can be imported; None comparisons are safe (None != any string).
    return action.source == source or action.canonical_source == source


def project_exposes_action(project: domain.CollectedProject, source: str) -> bool:
    """Whether a project defines an action under the given alias, without asking
    an ER to resolve it.

    Only the direct match of :func:`find_action_by_source` — selecting the
    projects a workspace-wide operation applies to must not cost one ER round
    trip per project that does not have the action.
    """
    return any(_matches_source(action, source) for action in project.actions)


async def find_action_by_source(
    actions: list[domain.Action],
    source: str,
    project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> domain.Action | None:
    """Find an action by an import-path alias (ADR-0019).

    Resolution is a two-step process:

    1. Direct match against the action's config ``source`` field and its
       ``canonical_source``.  This covers the vast majority of
       calls where callers use the same alias written in project configuration or
       the canonical path returned by ``actions/list``.

    2. If no match is found, ask a running ER to import the alias and return its
       canonical path, then retry the match
       against ``canonical_source``.  This covers arbitrary re-export aliases that
       resolve to the same class (full ADR-0019 support).  Envs that declare
       handlers for any of the project's known actions are tried first (they are
       guaranteed to have the relevant extension packages installed);
       ``dev_workspace`` and any other running runner are tried as a fallback.
    """
    # Step 1: direct match — covers the alias written in project config (source)
    # and callers that already hold the canonical path (canonical_source).
    action = next((a for a in actions if _matches_source(a, source)), None)
    if action is not None:
        return action

    # Step 2: ask an ER to resolve the alias.
    from finecode.wm_server.runner import runner_client as rc

    runners_by_env = ws_context.ws_projects_extension_runners.get(project.dir_path, {})

    # Prefer envs where handlers of known actions are declared — those envs are
    # guaranteed to have the relevant extension packages installed. Since we don't
    # yet know *which* action we're resolving, we collect handler envs across all
    # known actions. Fall back to dev_workspace, then any other running runner.
    seen_envs: set[str] = set()
    handler_envs: list[str] = []
    for a in actions:
        for h in a.handlers:
            if h.env not in seen_envs:
                seen_envs.add(h.env)
                handler_envs.append(h.env)
    env_order = handler_envs + [
        e
        for e in (
            ["dev_workspace"] + [e for e in runners_by_env if e != "dev_workspace"]
        )
        if e not in seen_envs
    ]
    for env_name in env_order:
        runner = runners_by_env.get(env_name)
        if runner is None or runner.status != rc.RunnerStatus.RUNNING:
            continue
        try:
            canonical = await rc.resolve_source(runner, source)
        except Exception as exc:
            logger.debug(
                f"find_action_by_source: ER '{env_name}' failed for '{source}': {exc}"
            )
            continue
        if canonical is None:
            continue
        action = next(
            (a for a in actions if a.canonical_source == canonical),
            None,
        )
        if action is not None:
            return action

    return None
