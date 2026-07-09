# docs: docs/cli.md
import json
import pathlib
import sys
import time
import typing
import uuid

import click
from loguru import logger
from finecode.wm_client import ApiClient, ApiError
from finecode.wm_server import wm_lifecycle
from finecode.wm_server.runner import runner_client
from finecode.cli_app import utils
from finecode.cli_app.log_render import render_log_records


class RunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _make_progress_handler(is_tty: bool) -> typing.Callable:
    """Return an async ``actions/progress`` notification handler.

    TTY: overwrites the current line in-place using ANSI escape sequences.
    Non-TTY: prints plain-text lines to stderr, throttled to avoid log flooding.
    """
    last_message: list[str] = [""]
    last_print_time: list[float] = [0.0]

    async def handler(params: dict) -> None:
        value = params.get("value", {}) if params else {}
        progress_type = value.get("type")

        if progress_type == "begin":
            title = value.get("title", "")
            if is_tty:
                click.echo(f"\r\033[K{title}...", nl=False, err=True)
            else:
                click.echo(f"Starting: {title}", err=True)
            last_message[0] = title

        elif progress_type == "report":
            message = value.get("message") or ""
            percentage = value.get("percentage")
            if not message:
                return
            now = time.monotonic()
            # Throttle to at most once per second for TTY; always print for non-TTY
            # unless the message hasn't changed.
            if is_tty:
                if now - last_print_time[0] < 1.0:
                    return
                last_print_time[0] = now
                if percentage is not None:
                    click.echo(f"\r\033[K{percentage}% {message}", nl=False, err=True)
                else:
                    click.echo(f"\r\033[K{message}", nl=False, err=True)
            else:
                if message == last_message[0] and now - last_print_time[0] < 1.0:
                    return
                last_print_time[0] = now
                last_message[0] = message
                if percentage is not None:
                    click.echo(f"{percentage}% {message}", err=True)
                else:
                    click.echo(message, err=True)

        elif progress_type == "end":
            if is_tty:
                click.echo("\r\033[K", nl=False, err=True)
            else:
                end_message = value.get("message")
                if end_message:
                    click.echo(f"Done: {end_message}", err=True)

    return handler


async def run_actions(
    workdir_path: pathlib.Path,
    projects_names: list[str] | None,
    actions: list[str],
    action_payload: dict[str, typing.Any],
    concurrently: bool,
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] | None = None,
    save_results: bool = True,
    map_payload_fields: set[str] | None = None,
    own_server: bool = False,
    log_level: str = "INFO",
    dev_env: str = "cli",
    wal_enabled: bool = False,
    verbose: bool = False,
) -> utils.RunActionsResult:
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(
                workdir_path,
                log_level=log_level,
                wal_enabled=wal_enabled,
            )
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise RunFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise RunFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        try:
            if handler_config_overrides:
                if own_server:
                    await client.set_config_overrides(handler_config_overrides)
                else:
                    click.echo(
                        "Warning: --config overrides are ignored in --shared-server mode. ",
                        err=True,
                    )
            # Tree-change notifications are irrelevant in CLI (run-and-exit) mode;
            # register a no-op before add_dir so notifications fired during project
            # loading don't hit the "unhandled notification" fallback.
            async def _ignore_tree_changed(params: dict) -> None:
                pass

            client.on_notification("actions/treeChanged", _ignore_tree_changed)

            async def _on_user_message(params: dict) -> None:
                value = params or {}
                click.echo(value.get("message", ""), err=True)

            client.on_notification("server/userMessage", _on_user_message)

            async def _on_log_records(params: dict) -> None:
                for line in render_log_records(params):
                    click.echo(line, err=True)

            if verbose:
                client.on_notification("server/logRecords", _on_log_records)
                # Stream WM+ER logs at the single general level (--log-level).
                await client.subscribe_logs(log_level)

            # When a project filter is given and we own the server, discover
            # projects first (no runners), resolve names to paths, then start
            # runners only for the requested projects.  In shared-server mode
            # runners are already running, so always use the normal path.
            deferred_runner_start = own_server and projects_names is not None
            logger.info("Initializing workspace...")
            try:
                await client.add_dir(
                    workdir_path,
                    start_runners=not deferred_runner_start,
                    initialize_all_handlers=not own_server,
                )
            except ApiError as exc:
                raise RunFailed(str(exc)) from exc

            # Resolve project names (CLI option) to paths (canonical API identifier).
            project_paths: list[str] | None = None
            if projects_names is not None:
                all_projects = await client.list_projects()
                unknown = [
                    n for n in projects_names
                    if not any(p["name"] == n for p in all_projects)
                ]
                if unknown:
                    raise RunFailed(f"Unknown project(s): {unknown}")
                project_paths = [
                    p["path"] for p in all_projects if p["name"] in projects_names
                ]

            if deferred_runner_start:
                try:
                    await client.start_runners(projects=project_paths)
                except ApiError as exc:
                    raise RunFailed(str(exc)) from exc

            # Resolve action names to sources (ADR-0019).
            all_actions = await client.list_actions()
            name_to_source: dict[str, str] = {a["name"]: a["source"] for a in all_actions}
            source_to_name: dict[str, str] = {a["source"]: a["name"] for a in all_actions}
            unknown_actions = [a for a in actions if a not in name_to_source]
            if unknown_actions:
                raise RunFailed(f"Unknown action(s): {unknown_actions}")
            action_sources = [name_to_source[a] for a in actions]

            # Workspace-scoped actions run once on the root project and stream all
            # their sub-project output tagged with that single root path.  Repeating
            # the root header for every partial adds no information, so suppress it
            # when every requested action is workspace-scoped.
            scope_by_source = {a["source"]: a.get("scope") for a in all_actions}
            show_project_header = not (
                action_sources
                and all(scope_by_source.get(src) == "workspace" for src in action_sources)
            )

            params_by_project: dict[str, dict[str, typing.Any]] = {}
            if map_payload_fields:
                params_by_project = _resolve_mapped_payload_fields(
                    map_payload_fields=map_payload_fields,
                    action_payload=action_payload,
                )

            result_formats = ["string", "json"] if save_results else ["string"]

            # Always stream via partial-result notifications, even for single-project runs.
            #
            # The non-streaming path (progress_token only) assumed: one project → one direct
            # result in result_by_format.  That holds for project-scope actions, but
            # workspace-scope actions (e.g. inspect_code) fan out to sub-projects internally
            # and deliver all output via partial_result_sender — the final RunActionResponse
            # has result_by_format={} regardless of how many --project filters are given.
            # Streaming works correctly for both cases, so there is no reason to branch.
            batch_options = {
                "concurrently": concurrently,
                "resultFormats": result_formats,
                "trigger": "user",
                "devEnv": dev_env,
                # Ask the WM to type-safely merge streamed partials per project/action
                # and return the merged result, so the returned/saved data is complete
                # even when one project streams many partials.
                "mergeResults": True,
            }

            partial_result_token = str(uuid.uuid4())

            async def _on_partial_result(params: dict) -> None:
                value = params.get("value", {}) if params else {}
                project_str = value.get("project", "")
                results = value.get("results", {})
                block = _format_project_block(
                    project_str, results, source_to_name, show_project_header
                )
                # A partial with no rendered content (e.g. a project with nothing to
                # report) would otherwise print just the project header with an empty
                # body; skip it. The merged result still lands in the final response.
                if block is None:
                    return

                # split blocks with newline
                block = "\n" + block

                click.echo(block, nl=False)

            client.on_notification("actions/partialResult", _on_partial_result)

            logger.info(f"Running {', '.join(actions)}...")
            try:
                batch_result = await client.run_batch(
                    action_sources=action_sources,
                    projects=project_paths,
                    params=action_payload,
                    params_by_project=params_by_project or None,
                    options=batch_options,
                    partial_result_token=partial_result_token,
                )
            except ApiError as exc:
                raise RunFailed(str(exc)) from exc

            # Use the WM's type-safely merged per-project results (requested via
            # mergeResults) for the saved/returned data.
            return _build_streaming_result(
                batch_result.get("results", {}), batch_result.get("returnCode", 0)
            )
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


def _format_project_block(
    project_path_str: str,
    actions_results: dict,
    source_to_name: dict[str, str] | None = None,
    show_project_header: bool = True,
) -> str | None:
    """Format one project's action results as a printable block.

    Prepends the project path header unless *show_project_header* is False — the
    caller suppresses it for workspace-scoped runs, where every partial carries
    the same root path and the header would just repeat.  Returns ``None`` when
    there is nothing to render, so the caller can skip printing an empty body.
    """
    run_many_actions = len(actions_results) > 1
    project_output_parts: list[str] = []

    for action_source, action_data in actions_results.items():
        result_by_format = action_data.get("resultByFormat", {})
        return_code = action_data.get("returnCode", 0)
        response = runner_client.RunActionResponse(
            result_by_format=result_by_format,
            return_code=return_code,
        )
        display_name = (source_to_name or {}).get(action_source, action_source)
        action_output = ""
        if run_many_actions:
            action_output += f"{click.style(display_name, bold=True)}:"
        action_output += utils.run_result_to_str(response.text(), display_name)
        project_output_parts.append(action_output)

    content = "".join(project_output_parts)
    if not content.strip():
        return None

    if show_project_header:
        block = f"{click.style(project_path_str, bold=True, underline=True)}\n" + content
    else:
        block = content

    if not block.endswith("\n"):
        block += "\n"

    return block


def _build_streaming_result(
    streaming_results: dict[str, dict],
    overall_return_code: int,
) -> utils.RunActionsResult:
    """Build a RunActionsResult from collected partial-result notifications.

    Output is empty because each project block was already printed to stdout as
    the notification arrived.  ``result_by_project`` is populated for callers
    that need the structured data (e.g. ``--save-results``).
    """
    result_by_project: dict[pathlib.Path, dict[str, runner_client.RunActionResponse]] = {}
    for project_path_str, actions_results in streaming_results.items():
        project_path = pathlib.Path(project_path_str)
        project_responses: dict[str, runner_client.RunActionResponse] = {}
        for action_source, action_data in actions_results.items():
            project_responses[action_source] = runner_client.RunActionResponse(
                result_by_format=action_data.get("resultByFormat", {}),
                return_code=action_data.get("returnCode", 0),
            )
        result_by_project[project_path] = project_responses

    return utils.RunActionsResult(
        output="",
        return_code=overall_return_code,
        result_by_project=result_by_project,
    )


def _resolve_mapped_payload_fields(
    map_payload_fields: set[str],
    action_payload: dict[str, typing.Any],
) -> dict[str, dict[str, typing.Any]]:
    """Resolve mapped payload fields from saved action results.

    Returns a dict keyed by project path string, where each value is a dict
    of field overrides for that project.
    """
    results_dir = pathlib.Path(sys.executable).parent.parent / "cache" / "finecode" / "results"
    params_by_project: dict[str, dict[str, typing.Any]] = {}

    for field_name in map_payload_fields:
        raw_value = action_payload.get(field_name)
        if raw_value is None:
            raise RunFailed(
                f"Mapped payload field '{field_name}' not found in action payload"
            )

        action_name, field_path = str(raw_value).split(".", 1)
        result_file = results_dir / f"{action_name}.json"
        if not result_file.exists():
            raise RunFailed(
                f"Results file '{result_file}' not found for mapped field '{field_name}'"
            )

        results_by_project: dict[str, typing.Any] = json.loads(result_file.read_text())
        for project_path, project_result in results_by_project.items():
            resolved_value = project_result
            for key in field_path.split("."):
                if not isinstance(resolved_value, dict):
                    raise RunFailed(
                        f"Cannot resolve '{field_path}' in results of '{action_name}'"
                        f" for project '{project_path}'"
                    )
                resolved_value = resolved_value.get(key)

            if project_path not in params_by_project:
                params_by_project[project_path] = {}
            params_by_project[project_path][field_name] = resolved_value

    return params_by_project


__all__ = ["run_actions"]
