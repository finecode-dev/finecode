# docs: docs/cli.md
import pathlib
import sys

from finecode.wm_client import ApiClient, ApiError  # ApiError used for start_runners check
from finecode.wm_server import wm_lifecycle
from finecode.cli_app.commands._env_setup import create_and_install_envs, EnvSetupFailed
from loguru import logger


class BootstrapFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def bootstrap(
    workdir_path: pathlib.Path,
    recreate: bool = False,
    log_level: str = "INFO",
) -> None:
    """Create the dev_workspace environment for the workspace root.

    Uses the current Python process (sys.executable) as a temporary runner so
    that ``create_envs`` and ``install_envs`` can run before the venv exists.
    After bootstrap, run ``finecode prepare-envs`` to set up all other envs.
    """
    port_file = None
    try:
        port_file = wm_lifecycle.start_own_server(workdir_path, log_level=log_level)
        try:
            port = await wm_lifecycle.wait_until_ready_from_file(port_file)
        except TimeoutError as exc:
            raise BootstrapFailed(str(exc)) from exc

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
    client: ApiClient,
    workdir_path: pathlib.Path,
    recreate: bool,
) -> None:
    venv_dir = workdir_path / ".venvs" / "dev_workspace"
    workdir_str = str(workdir_path)

    if venv_dir.exists() and not recreate:
        logger.info(
            f"dev_workspace already exists at '{venv_dir}'. "
            "Use --recreate to delete and recreate it."
        )
        return

    # Discover projects (no runners — venv may not exist yet).
    logger.info("Discovering projects...")
    result = await client.add_dir(workdir_path, start_runners=False)
    projects: list[dict] = result.get("projects", [])

    current_project = next((p for p in projects if p["path"] == workdir_str), None)
    if current_project is None:
        raise BootstrapFailed(
            "bootstrap must be run from the workspace/project root"
        )
    if current_project["status"] == "CONFIG_INVALID":
        raise BootstrapFailed(
            f"Project '{current_project['name']}' has invalid configuration"
        )

    if venv_dir.exists():
        # recreate=True (already handled False above)
        logger.info(f"Removing existing dev_workspace at '{venv_dir}'...")
        try:
            await client.remove_env(workdir_str, "dev_workspace")
        except ApiError as exc:
            raise BootstrapFailed(
                f"Failed to remove existing dev_workspace: {exc}"
            ) from exc

    # Start the dev_workspace runner using the current Python executable.
    # This works even though the venv doesn't exist yet because sys.executable
    # already has finecode and all handlers installed (e.g. via pipx/uvx).
    logger.info(f"Starting temporary runner using {sys.executable}...")
    try:
        await client.start_runners(
            projects=[workdir_str],
            python_overrides={"dev_workspace": sys.executable},
        )
    except ApiError as exc:
        raise BootstrapFailed(f"Failed to start runner: {exc}") from exc

    dw_env = {
        "name": "dev_workspace",
        "venv_dir_path": venv_dir.as_uri(),
        "project_def_path": (workdir_path / "pyproject.toml").as_uri(),
    }

    logger.info("Creating dev_workspace virtualenv and installing dependencies...")
    try:
        await create_and_install_envs(
            client=client,
            project_path=workdir_str,
            envs=[dw_env],
            dev_env="cli",
        )
    except EnvSetupFailed as exc:
        raise BootstrapFailed(exc.message) from exc

    logger.info(
        f"Bootstrap complete. dev_workspace created at '{venv_dir}'.\n"
        "Next step: run 'finecode prepare-envs' to set up all other environments."
    )


__all__ = ["bootstrap", "BootstrapFailed"]
