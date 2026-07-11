"""Type-safe merging of streamed partial results.
"""

from __future__ import annotations

import pathlib

from finecode.wm_server import context, domain

from .exceptions import ActionRunFailed

__all__ = ["merge_partial_results_for_action"]


async def merge_partial_results_for_action(
    project_path: pathlib.Path,
    action_name: str,
    json_payloads: list[dict],
    ws_context: context.WorkspaceContext,
) -> dict | None:
    """Type-safely merge streamed partial-result ``json`` payloads into one.

    Each entry in *json_payloads* is a ``dataclasses.asdict()`` of the action's
    ``RESULT_TYPE`` (the ``"json"`` format of a streamed partial).  Merging is
    delegated to the ER's ``actions/mergeResults`` command, which reconstructs
    the typed result and calls ``RunActionResult.update()`` — the same primitive
    the runner uses to combine handler results.  This is the only place the typed
    object is reconstructable, so merging must happen ER-side even though the WM
    only ever sees serialized dicts.

    Returns the merged ``json`` dict, or ``None`` when nothing was streamed for
    this slot.  Raises :class:`ActionRunFailed` when a merge is required but
    cannot be performed (no running runner, or the merge RPC fails): the caller
    opted into ``mergeResults`` precisely to obtain the complete result, so a
    silent fallback to a single partial would re-introduce the data loss this
    path exists to prevent.  Mirrors the merge-failure convention in
    ``proxy_utils`` multi-env merging.
    """
    from finecode.wm_server.runner import runner_client

    non_empty = [payload for payload in json_payloads if payload]
    if not non_empty:
        return None
    # A single payload needs no merge — return it directly (no runner required).
    if len(non_empty) == 1:
        return non_empty[0]

    project_def = ws_context.ws_projects.get(project_path)
    if not isinstance(project_def, domain.CollectedProject):
        raise ActionRunFailed(
            f"Cannot merge {len(non_empty)} partial results for action "
            f"'{action_name}': project '{project_path}' has no collected actions"
        )
    action_def = next((a for a in project_def.actions if a.name == action_name), None)
    if action_def is None:
        raise ActionRunFailed(
            f"Cannot merge partial results for action '{action_name}': "
            f"action not found in project '{project_path}'"
        )

    runners_by_env = ws_context.ws_projects_extension_runners.get(project_path, {})
    merge_runner: runner_client.ExtensionRunnerInfo | None = None
    for handler in action_def.handlers:
        candidate = runners_by_env.get(handler.env)
        if candidate is not None and candidate.status == runner_client.RunnerStatus.RUNNING:
            merge_runner = candidate
            break

    if merge_runner is None:
        raise ActionRunFailed(
            f"Cannot merge {len(non_empty)} partial results for action "
            f"'{action_name}' in project '{project_path}': no running runner among "
            f"handler envs {[h.env for h in action_def.handlers]}"
        )

    try:
        return await runner_client.merge_results(
            runner=merge_runner,
            action_name=action_name,
            results=non_empty,
        )
    except runner_client.BaseRunnerRequestException as exc:
        raise ActionRunFailed(
            f"merge_results failed for {action_name} in project '{project_path}': "
            f"{exc.message}"
        ) from exc
