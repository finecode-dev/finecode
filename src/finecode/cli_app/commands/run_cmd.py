import json
import pathlib
import sys
import typing

import click

from finecode.api_client import ApiClient, ApiError
from finecode.api_server import api_server
from finecode.api_server.runner import runner_client
from finecode.cli_app import utils


class RunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


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
) -> utils.RunActionsResult:
    port_file = None
    try:
        if own_server:
            port_file = api_server.start_own_server(workdir_path)
            try:
                port = await api_server.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise RunFailed(str(exc)) from exc
        else:
            api_server.ensure_running(workdir_path)
            try:
                port = await api_server.wait_until_ready()
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
            # TODO: could it be optimized: if projects are provided, parse only them?
            # the same also in other CLI commands
            await client.add_dir(workdir_path)

            params_by_project: dict[str, dict[str, typing.Any]] = {}
            if map_payload_fields:
                params_by_project = _resolve_mapped_payload_fields(
                    map_payload_fields=map_payload_fields,
                    action_payload=action_payload,
                )

            result_formats = ["string", "json"] if save_results else ["string"]

            try:
                batch_result = await client.run_batch(
                    actions=actions,
                    projects=projects_names,
                    params=action_payload,
                    params_by_project=params_by_project or None,
                    options={
                        "concurrently": concurrently,
                        "result_formats": result_formats,
                        "trigger": "user",
                        "dev_env": "cli",
                    },
                )
            except ApiError as exc:
                raise RunFailed(str(exc)) from exc

            return _build_run_result(batch_result)
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


def _build_run_result(batch_result: dict) -> utils.RunActionsResult:
    """Convert the actions/runBatch API response to RunActionsResult."""
    raw_results: dict[str, dict] = batch_result.get("results", {})
    overall_return_code: int = batch_result.get("return_code", 0)

    result_by_project: dict[pathlib.Path, dict[str, runner_client.RunActionResponse]] = {}
    output_parts: list[str] = []

    run_in_many_projects = len(raw_results) > 1

    for project_path_str, actions_results in raw_results.items():
        project_path = pathlib.Path(project_path_str)
        project_responses: dict[str, runner_client.RunActionResponse] = {}

        project_output_parts: list[str] = []
        run_many_actions = len(actions_results) > 1

        for action_name, action_data in actions_results.items():
            result_by_format = action_data.get("result_by_format", {})
            return_code = action_data.get("return_code", 0)

            response = runner_client.RunActionResponse(
                result_by_format=result_by_format,
                return_code=return_code,
            )
            project_responses[action_name] = response

            action_output = ""
            if run_many_actions:
                action_output += f"{click.style(action_name, bold=True)}:"
            action_output += utils.run_result_to_str(response.text(), action_name)
            project_output_parts.append(action_output)

        result_by_project[project_path] = project_responses

        project_block = "".join(project_output_parts)
        if run_in_many_projects:
            project_block = (
                f"{click.style(project_path_str, bold=True, underline=True)}\n"
                + project_block
            )
        output_parts.append(project_block)

    return utils.RunActionsResult(
        output="\n".join(output_parts),
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
