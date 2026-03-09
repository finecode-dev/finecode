import pathlib

from loguru import logger

from finecode.wm_client import ApiClient, ApiError
from finecode.wm_server import wm_server


class PrepareEnvsFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def prepare_envs(
    workdir_path: pathlib.Path, recreate: bool, own_server: bool = True
) -> None:
    """Prepare all virtual environments for a workspace.

    Orchestration steps:
    1. Discover projects (without starting runners — envs may not exist yet).
    2. Check / remove dev_workspace environments as needed.
    3. Run ``prepare_dev_workspaces_envs`` to create / update them.
    4. Start extension runners (resolves preset actions).
    5. Run ``prepare_runners`` to set up handler environments.
    6. Run ``prepare_envs`` to finalise environment setup.
    """
    port_file = None
    try:
        if own_server:
            port_file = wm_server.start_own_server(workdir_path)
            try:
                port = await wm_server.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc
        else:
            wm_server.ensure_running(workdir_path)
            try:
                port = await wm_server.wait_until_ready()
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        try:
            await _run(client, workdir_path, recreate)
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


async def _run(
    client: ApiClient, workdir_path: pathlib.Path, recreate: bool
) -> None:
    # Step 1 — discover projects without starting runners (envs may not exist).
    logger.info("Discovering projects...")
    result = await client.add_dir(workdir_path, start_runners=False)
    projects: list[dict] = result.get("projects", [])

    workdir_str = str(workdir_path)
    current_project = next(
        (p for p in projects if p["path"] == workdir_str), None
    )
    if current_project is None:
        raise PrepareEnvsFailed(
            "prepare-envs can be run only from workspace/project root"
        )

    invalid_status_projects = [
        p for p in projects if p["status"] == "CONFIG_INVALID"
    ]
    if invalid_status_projects:
        names = [p["name"] for p in invalid_status_projects]
        raise PrepareEnvsFailed(
            f"Projects have invalid configuration: {names}"
        )

    other_projects = [
        p
        for p in projects
        if p["path"] != workdir_str and p["status"] == "CONFIG_VALID"
    ]

    logger.info(f"Found {len(projects)} project(s): {[p['name'] for p in projects]}")

    # Step 2 — check / remove dev_workspace environments.
    logger.info("Checking dev workspace environments...")
    for project in other_projects:
        if recreate:
            logger.trace(
                f"Recreate env 'dev_workspace' in project '{project['name']}'"
            )
            try:
                await client.remove_env(project["name"], "dev_workspace")
            except ApiError as exc:
                raise PrepareEnvsFailed(
                    f"Failed to remove env for '{project['name']}': {exc}"
                ) from exc
        else:
            try:
                valid = await client.check_env(project["name"], "dev_workspace")
            except ApiError as exc:
                raise PrepareEnvsFailed(
                    f"Failed to check env for '{project['name']}': {exc}"
                ) from exc
            if not valid:
                logger.warning(
                    f"Env 'dev_workspace' in project '{project['name']}' is "
                    f"invalid, recreating it"
                )
                try:
                    await client.remove_env(project["name"], "dev_workspace")
                except ApiError as exc:
                    raise PrepareEnvsFailed(
                        f"Failed to remove invalid env for '{project['name']}': {exc}"
                    ) from exc

    # Step 3 — create / update dev_workspace environments.
    logger.info("Creating/updating dev workspace environments...")
    envs = [
        {
            "name": "dev_workspace",
            "venv_dir_path": str(pathlib.Path(p["path"]) / ".venvs" / "dev_workspace"),
            "project_def_path": str(pathlib.Path(p["path"]) / "pyproject.toml"),
        }
        for p in other_projects
    ]

    try:
        prepare_dw_result = await client.run_action(
            action="prepare_dev_workspaces_envs",
            project=current_project["name"],
            params={"envs": envs},
            options={
                "result_formats": ["string"],
                "trigger": "user",
                "dev_env": "cli",
            },
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(
            f"'prepare_dev_workspaces_envs' failed: {exc}"
        ) from exc

    if prepare_dw_result.get("return_code", 0) != 0:
        output = (prepare_dw_result.get("result_by_format") or {}).get("string", "")
        raise PrepareEnvsFailed(
            f"'prepare_dev_workspaces_envs' failed with return code "
            f"{prepare_dw_result['return_code']}: {output}"
        )

    # Step 4 — start runners with presets (resolves preset-defined actions).
    logger.info("Starting extension runners...")
    try:
        await client.start_runners()
    except ApiError as exc:
        raise PrepareEnvsFailed(f"Starting runners failed: {exc}") from exc

    # Steps 5 & 6 — run prepare_runners then prepare_envs on all projects.
    logger.info("Preparing runner and handler environments...")
    # Actions run sequentially within each project (prepare_runners before
    # prepare_envs), while projects run concurrently.
    try:
        batch_result = await client.run_batch(
            actions=["prepare_runners", "prepare_envs"],
            options={
                "concurrently": False,
                "result_formats": ["string"],
                "trigger": "user",
                "dev_env": "cli",
            },
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(f"'prepare_runners'/'prepare_envs' failed: {exc}") from exc

    if batch_result.get("return_code", 0) != 0:
        output_parts = []
        for actions_result in batch_result.get("results", {}).values():
            for response in actions_result.values():
                text = (response.get("result_by_format") or {}).get("string", "")
                if text:
                    output_parts.append(text)
        raise PrepareEnvsFailed(
            "'prepare_runners'/'prepare_envs' failed:\n" + "\n".join(output_parts)
        )
