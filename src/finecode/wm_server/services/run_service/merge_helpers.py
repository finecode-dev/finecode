"""Type-safely merge streamed partial results into one action result."""

from __future__ import annotations

import pathlib

from finecode.wm_server import context, domain
from finecode.wm_server.runner import _internal_client_types, runner_client

from .exceptions import ActionRunFailed

__all__ = ["merge_slot_results", "strip_unrequested_formats"]


async def merge_slot_results(
    project_path: pathlib.Path,
    action_name: str,
    entries: list[_internal_client_types.ResultByFormat],
    requested_formats: list[runner_client.RunResultFormat],
    ws_context: context.WorkspaceContext,
) -> _internal_client_types.ResultByFormat | None:
    """Merge one slot's partial results and return the requested formats."""
    counted_entries = [
        entry
        for entry in entries
        if "string" in entry or "styled_text_json" in entry or entry.get("json")
    ]
    if not counted_entries:
        return None
    if len(counted_entries) == 1:
        return strip_unrequested_formats(counted_entries[0], requested_formats)

    project_def = ws_context.ws_projects.get(project_path)
    if not isinstance(project_def, domain.CollectedProject):
        raise ActionRunFailed(
            f"Cannot merge {len(counted_entries)} partial results for action "
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
        if (
            candidate is not None
            and candidate.status == runner_client.RunnerStatus.RUNNING
        ):
            merge_runner = candidate
            break

    if merge_runner is None:
        raise ActionRunFailed(
            f"Cannot merge {len(counted_entries)} partial results for action "
            f"'{action_name}' in project '{project_path}': no running runner among "
            f"handler envs {[h.env for h in action_def.handlers]}"
        )

    formats_for_merge = list(requested_formats)
    if runner_client.RunResultFormat.JSON not in formats_for_merge:
        formats_for_merge.append(runner_client.RunResultFormat.JSON)
    json_payloads = [entry["json"] for entry in counted_entries if entry.get("json")]

    try:
        result_by_format = await runner_client.merge_results(
            runner=merge_runner,
            action_name=action_name,
            results=json_payloads,
            result_formats=formats_for_merge,
        )
        return strip_unrequested_formats(result_by_format, requested_formats)
    except runner_client.BaseRunnerRequestException as exc:
        raise ActionRunFailed(
            f"merge_results failed for {action_name} in project '{project_path}': "
            f"{exc.message}"
        ) from exc


def strip_unrequested_formats(
    entry: _internal_client_types.ResultByFormat,
    requested_formats: list[runner_client.RunResultFormat],
) -> _internal_client_types.ResultByFormat:
    """Return only formats requested by the client."""
    result: _internal_client_types.ResultByFormat = {}
    if runner_client.RunResultFormat.JSON in requested_formats and "json" in entry:
        result["json"] = entry["json"]
    if runner_client.RunResultFormat.STRING in requested_formats:
        if "string" in entry:
            result["string"] = entry["string"]
        elif "styled_text_json" in entry:
            result["styled_text_json"] = entry["styled_text_json"]
    return result
