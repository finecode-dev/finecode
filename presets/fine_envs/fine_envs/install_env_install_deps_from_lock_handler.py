import dataclasses
import pathlib

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from fine_envs import (
    install_deps_in_env_action,
    install_env_action,
)
from fine_envs.install_envs_action import (
    InstallEnvsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class InstallEnvInstallDepsFromLockHandlerConfig(
    code_action.ActionHandlerConfig
): ...


class InstallEnvInstallDepsFromLockHandler(
    code_action.ActionHandler[
        install_env_action.InstallEnvAction,
        InstallEnvInstallDepsFromLockHandlerConfig,
    ]
):
    """Install dependencies for a single environment from a lock file (e.g. pylock.toml)."""

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: install_env_action.InstallEnvRunPayload,
        run_context: install_env_action.InstallEnvRunContext,
    ) -> InstallEnvsRunResult:
        env = payload.env
        project_def_path = resource_uri_to_path(env.project_def_path)
        project_dir_path = project_def_path.parent
        lock_file_path = project_dir_path / f"pylock.{env.name}.toml"

        if not lock_file_path.exists():
            self.logger.warning(f"Lock file not found: {lock_file_path}, skipping")
            return InstallEnvsRunResult(errors=[])

        async with run_context.progress(f"Installing {env.name}") as progress:
            await progress.report("Reading lock file")
            dependencies = _parse_lock_file(lock_file_path)

            install_deps_payload = install_deps_in_env_action.InstallDepsInEnvRunPayload(
                env_name=env.name,
                venv_dir_path=env.venv_dir_path,
                project_dir_path=path_to_resource_uri(project_dir_path),
                dependencies=dependencies,
            )

            await progress.report("Installing dependencies")
            result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(install_deps_in_env_action.InstallDepsInEnvAction),
                payload=install_deps_payload,
                meta=run_context.meta,
            )
            return InstallEnvsRunResult(errors=result.errors)


def _parse_lock_file(
    lock_file_path: pathlib.Path,
) -> list[install_deps_in_env_action.Dependency]:
    with open(lock_file_path, "rb") as f:
        lock_data = tomllib.load(f)

    dependencies: list[install_deps_in_env_action.Dependency] = []
    for package in lock_data.get("packages", []):
        name = package["name"]
        version = package.get("version")

        if version is not None:
            version_or_source = f"=={version}"
        else:
            # packages without version (e.g. VCS or directory sources)
            continue

        editable = False
        directory = package.get("directory")
        if directory is not None:
            # directory.path is relative to the lock file location
            raw_path = pathlib.Path(directory.get("path", ""))
            resolved_path = (lock_file_path.parent / raw_path).resolve()
            editable = directory.get("editable", False)
            version_or_source = f" @ file://{resolved_path}"

        dependencies.append(
            install_deps_in_env_action.Dependency(
                name=name,
                version_or_source=version_or_source,
                editable=editable,
            )
        )

    return dependencies
