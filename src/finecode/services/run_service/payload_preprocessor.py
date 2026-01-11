import pathlib
import typing

from finecode import context


async def preprocess_for_project(
    action_name: str,
    payload: dict[str, typing.Any],
    project_dir_path: pathlib.Path,
    ws_context: context.WorkspaceContext,
) -> dict[str, typing.Any]:
    processed_payload = payload.copy()

    if action_name == "prepare_envs" or action_name == "prepare_runners":
        runtime_venv_path = project_dir_path / ".venvs" / "runtime"
        project_def_path = project_dir_path / "pyproject.toml"
        envs = [
            {
                "name": "runtime",
                "venv_dir_path": runtime_venv_path,
                "project_def_path": project_def_path,
            }
        ]
        # current approach: there are 4 default environments: runtime, dev_workspace,
        # dev, dev_no_runtime. `runtime` is created always, all other only if dependency
        # group for them exist.
        # In future there will be possibility to create additional envs and to configure
        # default ones.
        project_raw_config = ws_context.ws_projects_raw_configs[project_dir_path]
        deps_groups = project_raw_config.get("dependency-groups", {})
        # `dev_workspace` is handled separately in `prepare_env`, no need to include
        # here
        for default_env in ["dev", "dev_no_runtime"]:
            if default_env in deps_groups:
                venv_path = project_dir_path / ".venvs" / default_env
                envs.append(
                    {
                        "name": default_env,
                        "venv_dir_path": venv_path,
                        "project_def_path": project_def_path,
                    }
                )
        processed_payload["envs"] = envs

    return processed_payload
