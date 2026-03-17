# docs: docs/cli.md
import asyncio
import pathlib

from finecode.wm_client import ApiClient, ApiError
from finecode.wm_server import wm_lifecycle
from loguru import logger


class PrepareEnvsFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def prepare_envs(
    workdir_path: pathlib.Path,
    recreate: bool,
    own_server: bool = True,
    log_level: str = "INFO",
    dev_env: str = "cli",
    env_names: list[str] | None = None,
    project_names: list[str] | None = None,
) -> None:
    """Prepare all virtual environments for a workspace.

    Orchestration steps:
    1. Discover projects (without starting runners — envs may not exist yet).
    2. Check / remove dev_workspace environments as needed.
    3. Run ``create_envs`` + ``prepare_handler_envs`` to create / update dev_workspace envs.
    4. Start extension runners (resolves preset actions).
    5. Run ``create_envs`` to create all virtualenvs.
    6. Run ``prepare_runner_envs`` to install Extension Runners.
    7. Run ``prepare_handler_envs`` to install handler dependencies.

    When ``env_names`` is given only those named environments are prepared in
    step 7 (steps 5 and 6 still run for all envs).
    When ``project_names`` is given only those projects are prepared in steps 3, 5, 6, and 7.
    """
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(workdir_path, log_level=log_level)
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        try:
            await _run(
                client, workdir_path, recreate, dev_env, env_names, project_names
            )
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


def _check_batch_result(batch_result: dict, error_prefix: str) -> None:
    if batch_result.get("returnCode", 0) != 0:
        output_parts = []
        for actions_result in batch_result.get("results", {}).values():
            for response in actions_result.values():
                text = (response.get("resultByFormat") or {}).get("string", "")
                if text:
                    output_parts.append(text)
        raise PrepareEnvsFailed(error_prefix + ":\n" + "\n".join(output_parts))


async def _run(
    client: ApiClient,
    workdir_path: pathlib.Path,
    recreate: bool,
    dev_env: str = "cli",
    env_names: list[str] | None = None,
    project_names: list[str] | None = None,
) -> None:
    # Step 1 — discover projects without starting runners (envs may not exist).
    logger.info("Discovering projects...")
    result = await client.add_dir(workdir_path, start_runners=False)
    projects: list[dict] = result.get("projects", [])

    workdir_str = str(workdir_path)
    current_project = next((p for p in projects if p["path"] == workdir_str), None)
    if current_project is None:
        raise PrepareEnvsFailed(
            "prepare-envs can be run only from workspace/project root"
        )

    invalid_status_projects = [p for p in projects if p["status"] == "CONFIG_INVALID"]
    if invalid_status_projects:
        names = [p["name"] for p in invalid_status_projects]
        raise PrepareEnvsFailed(f"Projects have invalid configuration: {names}")

    other_projects = [
        p
        for p in projects
        if p["path"] != workdir_str and p["status"] == "CONFIG_VALID"
    ]

    project_paths: list[str] | None = None
    if project_names is not None:
        unknown = [
            n for n in project_names if not any(p["name"] == n for p in projects)
        ]
        if unknown:
            raise PrepareEnvsFailed(f"Unknown project(s): {unknown}")
        other_projects = [p for p in other_projects if p["name"] in project_names]
        # Resolve names to paths for all subsequent API calls (canonical identifier)
        project_paths = [p["path"] for p in projects if p["name"] in project_names]

    logger.info(f"Found {len(projects)} project(s): {[p['name'] for p in projects]}")

    # Step 2 — check / remove dev_workspace environments (parallelized).
    logger.info("Checking dev workspace environments...")

    async def _check_or_remove_dw(project: dict) -> None:
        if recreate:
            logger.trace(f"Recreate env 'dev_workspace' in project '{project['name']}'")
            try:
                await client.remove_env(project["path"], "dev_workspace")
            except ApiError as exc:
                raise PrepareEnvsFailed(
                    f"Failed to remove env for '{project['name']}': {exc}"
                ) from exc
        else:
            try:
                valid = await client.check_env(project["path"], "dev_workspace")
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
                    await client.remove_env(project["path"], "dev_workspace")
                except ApiError as exc:
                    raise PrepareEnvsFailed(
                        f"Failed to remove invalid env for '{project['name']}': {exc}"
                    ) from exc

    try:
        async with asyncio.TaskGroup() as tg:
            for project in other_projects:
                tg.create_task(_check_or_remove_dw(project))
    except* PrepareEnvsFailed as eg:
        raise eg.exceptions[0]

    # Step 3 — create / update dev_workspace environments.
    logger.info("Creating/updating dev workspace environments...")
    dw_envs = [
        {
            "name": "dev_workspace",
            "venv_dir_path": str(pathlib.Path(p["path"]) / ".venvs" / "dev_workspace"),
            "project_def_path": str(pathlib.Path(p["path"]) / "pyproject.toml"),
        }
        for p in other_projects
    ]
    dw_options = {
        "resultFormats": ["string"],
        "trigger": "user",
        "devEnv": dev_env,
    }

    # Step 3a — create the dev_workspace virtualenvs.
    try:
        create_dw_result = await client.run_action(
            action="create_envs",
            project=current_project["path"],
            # 'recreate' is handled for dev_workspace envs above, no need to pass here
            params={"envs": dw_envs},
            options=dw_options,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(f"'create_envs' (dev_workspace) failed: {exc}") from exc
    if create_dw_result.get("returnCode", 0) != 0:
        output = (create_dw_result.get("resultByFormat") or {}).get("string", "")
        raise PrepareEnvsFailed(
            f"'create_envs' (dev_workspace) failed with return code "
            f"{create_dw_result['returnCode']}: {output}"
        )

    # Step 3b — install dev_workspace dependencies.
    try:
        prepare_dw_result = await client.run_action(
            action="prepare_handler_envs",
            project=current_project["path"],
            params={"envs": dw_envs},
            options=dw_options,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(
            f"'prepare_handler_envs' (dev_workspace) failed: {exc}"
        ) from exc
    if prepare_dw_result.get("returnCode", 0) != 0:
        output = (prepare_dw_result.get("resultByFormat") or {}).get("string", "")
        raise PrepareEnvsFailed(
            f"'prepare_handler_envs' (dev_workspace) failed with return code "
            f"{prepare_dw_result['returnCode']}: {output}"
        )

    # Step 4 — start runners with presets (resolves preset-defined actions).
    logger.info("Starting extension runners...")
    try:
        await client.start_runners()
    except ApiError as exc:
        raise PrepareEnvsFailed(f"Starting runners failed: {exc}") from exc

    # Steps 5, 6 & 7 — create envs, install runners, install handler deps.
    logger.info("Creating envs and installing runner and handler dependencies...")
    # Each step runs across all projects concurrently.
    common_options = {
        "concurrently": False,
        "resultFormats": ["string"],
        "trigger": "user",
        "devEnv": dev_env,
    }

    # Step 5 — create all virtualenvs (no env filter).
    try:
        create_result = await client.run_batch(
            actions=["create_envs"],
            projects=project_paths,
            options=common_options,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(f"'create_envs' failed: {exc}") from exc
    _check_batch_result(create_result, "'create_envs' failed")

    # Step 6 — install Extension Runners (no env filter).
    try:
        runners_result = await client.run_batch(
            actions=["prepare_runner_envs"],
            projects=project_paths,
            options=common_options,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(f"'prepare_runner_envs' failed: {exc}") from exc
    _check_batch_result(runners_result, "'prepare_runner_envs' failed")

    # Step 7 — install handler dependencies (with optional env_names filter).
    handler_params = {"env_names": env_names} if env_names is not None else {}
    try:
        batch_result = await client.run_batch(
            actions=["prepare_handler_envs"],
            projects=project_paths,
            params=handler_params,
            options=common_options,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(f"'prepare_handler_envs' failed: {exc}") from exc
    _check_batch_result(batch_result, "'prepare_handler_envs' failed")


__all__ = ["prepare_envs", "PrepareEnvsFailed"]
