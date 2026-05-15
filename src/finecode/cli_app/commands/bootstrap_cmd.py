# docs: docs/cli.md
import pathlib
import shutil
import sys
import tomllib

from finecode.wm_client import ApiClient, ApiError  # ApiError used for start_runners check
from finecode.wm_server import wm_lifecycle
from finecode.cli_app.commands._env_setup import create_and_install_envs, EnvSetupFailed
from loguru import logger


class BootstrapFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _raise_no_finecode_error(pyproject_path: pathlib.Path) -> None:
    """Read pyproject.toml and raise BootstrapFailed with a specific message."""
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)
    dep_groups = pyproject.get("dependency-groups", {})
    if "dev_workspace" not in dep_groups:
        raise BootstrapFailed(
            "The 'dev_workspace' dependency group is missing from pyproject.toml. "
            "Add '[dependency-groups]\\ndev_workspace = [\"finecode\"]' to pyproject.toml."
        )
    dw_deps = dep_groups.get("dev_workspace", [])
    has_finecode = any(
        isinstance(dep, str) and dep.split("[")[0].strip().lower() == "finecode"
        for dep in dw_deps
    )
    if not has_finecode:
        raise BootstrapFailed(
            "'finecode' is not listed in the 'dev_workspace' dependency group in pyproject.toml."
        )
    raise BootstrapFailed(
        "Project has no finecode configuration. "
        "Add '[tool.finecode]' to pyproject.toml or create a finecode.toml file."
    )


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
        # Silence "unhandled notification" trace log — treeChanged is irrelevant in CLI mode.
        async def _noop(_: object) -> None: pass
        client.on_notification("actions/treeChanged", _noop)
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

    pyproject_path = workdir_path / "pyproject.toml"
    if not pyproject_path.exists():
        raise BootstrapFailed(
            f"pyproject.toml not found: bootstrap must be run from the workspace/project root"
        )

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
    if current_project["status"] == "NO_FINECODE":
        _raise_no_finecode_error(pyproject_path)

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
            resolve_presets=False,
        )
    except ApiError as exc:
        raise BootstrapFailed(f"Failed to start runner: {exc}") from exc

    # The runner process creates .venvs/dev_workspace/logs/ for its own log
    # files. If that stub directory exists but is not a proper venv (no Python
    # binary), remove it so the env creator can build a real venv there.
    if venv_dir.exists():
        venv_python = venv_dir / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        if not venv_python.exists():
            logger.debug(f"Removing stub directory '{venv_dir}' left by runner log setup")
            shutil.rmtree(venv_dir)

    dw_env = {
        "name": "dev_workspace",
        "venv_dir_path": venv_dir.as_uri(),
        "project_def_path": pyproject_path.as_uri(),
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
