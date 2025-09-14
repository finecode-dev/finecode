import pathlib

import click

from finecode import context
from finecode.services import run_service


def run_result_to_str(
    run_result: str | dict[str, list[str | dict[str, str | bool]]], action_name: str
) -> str:
    run_result_str = ""
    if isinstance(run_result, str):
        run_result_str = run_result
    elif isinstance(run_result, dict):
        # styled text
        text_parts = run_result.get("parts", [])
        if not isinstance(text_parts, list):
            raise run_service.ActionRunFailed(
                f"Running of action {action_name} failed: got unexpected result, 'parts' value expected to be a list."
            )

        for text_part in text_parts:
            if isinstance(text_part, str):
                run_result_str += text_part
            elif isinstance(text_part, dict):
                try:
                    text = text_part["text"]
                except KeyError:
                    raise run_service.ActionRunFailed(
                        f"Running of action {action_name} failed: got unexpected result, 'text' value is required in object with styled text params."
                    )

                style_params: dict[str, str | bool] = {}
                if "foreground" in text_part and isinstance(
                    text_part["foreground"], str
                ):
                    style_params["fg"] = text_part["foreground"]

                if "background" in text_part and isinstance(
                    text_part["background"], str
                ):
                    style_params["bg"] = text_part["background"]

                if "bold" in text_part and isinstance(text_part["bold"], bool):
                    style_params["bold"] = text_part["bold"]

                if "underline" in text_part and isinstance(
                    text_part["underline"], bool
                ):
                    style_params["underline"] = text_part["underline"]

                if "overline" in text_part and isinstance(text_part["overline"], bool):
                    style_params["overline"] = text_part["overline"]

                if "italic" in text_part and isinstance(text_part["italic"], bool):
                    style_params["italic"] = text_part["italic"]

                if "blink" in text_part and isinstance(text_part["blink"], bool):
                    style_params["blink"] = text_part["blink"]

                if "strikethrough" in text_part and isinstance(
                    text_part["strikethrough"], bool
                ):
                    style_params["strikethrough"] = text_part["strikethrough"]

                if "reset" in text_part and isinstance(text_part["reset"], bool):
                    style_params["reset"] = text_part["reset"]

                run_result_str += click.style(text, **style_params)
            else:
                raise run_service.ActionRunFailed(
                    f"Running of action {action_name} failed: got unexpected result, 'parts' list can contain only strings or objects with styled text."
                )

    return run_result_str


async def run_actions_in_projects_and_concat_results(
    actions_by_project: dict[pathlib.Path, list[str]],
    action_payload: dict[str, str],
    ws_context: context.WorkspaceContext,
    concurrently: bool,
) -> tuple[str, int]:
    result_by_project = await run_service.run_actions_in_projects(
        actions_by_project=actions_by_project,
        action_payload=action_payload,
        ws_context=ws_context,
        concurrently=concurrently,
        result_format=run_service.RunResultFormat.STRING,
    )

    result_output: str = ""
    result_return_code: int = 0

    run_in_many_projects = len(result_by_project) > 1
    is_first_project = True
    for project_dir_path, result_by_action in result_by_project.items():
        run_many_actions = len(result_by_action) > 1

        if not is_first_project:
            result_output += "\n"

        if run_in_many_projects:
            result_output += (
                f"{click.style(str(project_dir_path), bold=True, underline=True)}\n"
            )

        for action_name, action_result in result_by_action.items():
            if run_many_actions:
                result_output += f"{click.style(action_name, bold=True)}:"
            action_result_str = run_result_to_str(action_result.result, action_name)
            result_output += action_result_str
            result_return_code |= action_result.return_code

        if is_first_project:
            is_first_project = False

    return (result_output, result_return_code)
