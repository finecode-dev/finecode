# docs: docs/cli.md
import pathlib

from finecode.wm_client import ApiClient, ApiError
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode.wm_server import wm_lifecycle


class DumpFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def dump_config(
    workdir_path: pathlib.Path, project_name: str, own_server: bool = True, log_level: str = "INFO", dev_env: str = "cli"
):
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(workdir_path, log_level=log_level)
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise DumpFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise DumpFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        try:
            result = await client.add_dir(workdir_path)
            projects = result.get("projects", [])
            project = next(
                (p for p in projects if p["name"] == project_name), None
            )
            if project is None:
                raise DumpFailed(f"Project '{project_name}' not found")

            project_path = project["path"]
            project_dir_path = pathlib.Path(project_path)
            source_file_path = project_dir_path / "pyproject.toml"
            target_file_path = project_dir_path / "finecode_config_dump" / "pyproject.toml"

            try:
                project_raw_config = await client.get_project_raw_config(project_path)
                await client.run_action(
                    action="dump_config",
                    project=project_path,
                    params={
                        "source_file_path": str(
                            path_to_resource_uri(source_file_path)
                        ),
                        "project_raw_config": project_raw_config,
                        "target_file_path": str(
                            path_to_resource_uri(target_file_path)
                        ),
                    },
                    options={
                        "resultFormats": ["string"],
                        "trigger": "user",
                        "devEnv": dev_env,
                    },
                )
            except ApiError as exc:
                raise DumpFailed(str(exc)) from exc
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)
