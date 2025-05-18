import pathlib
import typing

from finecode.workspace_manager import project_analyzer


def preprocess_for_project(
    action_name: str, payload: dict[str, typing.Any], project_dir_path: pathlib.Path
) -> dict[str, typing.Any]:
    processed_payload = payload.copy()

    # temporary hardcore logic until we get the proper payload structure and defaults
    # from extension runner
    if action_name == "lint" or action_name == "format":
        if "file_paths" not in processed_payload:
            processed_payload["file_paths"] = None
        if "save" not in processed_payload:
            processed_payload["save"] = True

    for param, value in processed_payload.items():
        if param == "file_paths" and value is None:
            processed_payload["file_paths"] = project_analyzer.get_project_files(
                project_dir_path
            )

    return processed_payload
