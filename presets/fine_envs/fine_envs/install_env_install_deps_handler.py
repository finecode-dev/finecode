import dataclasses

from packaging.utils import canonicalize_name

from finecode_extension_api import code_action
from fine_envs import (
    install_deps_in_env_action,
    install_env_action,
)
from fine_envs.install_envs_action import (
    InstallEnvsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner, iprojectinfoprovider
from finecode_extension_api.resource_uri import path_to_resource_uri, resource_uri_to_path
from fine_envs.dependency_config_utils import (
    collect_transitive_editable_deps,
    get_dependency_name,
    process_raw_deps,
)


@dataclasses.dataclass
class InstallEnvInstallDepsHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallEnvInstallDepsHandler(
    code_action.ActionHandler[
        install_env_action.InstallEnvAction,
        InstallEnvInstallDepsHandlerConfig,
    ]
):
    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: install_env_action.InstallEnvRunPayload,
        run_context: install_env_action.InstallEnvRunContext,
    ) -> InstallEnvsRunResult:
        env = payload.env
        project_def = run_context.project_def
        if project_def is None:
            raise code_action.ActionFailedException(
                "project_def must be set by InstallEnvReadConfigHandler"
            )

        async with run_context.progress(f"Installing {env.name}") as progress:
            await progress.report("Reading configuration")
            deps_groups = project_def.get("dependency-groups", {})
            env_raw_deps = deps_groups.get(env.name, [])
            project_def_path = resource_uri_to_path(env.project_def_path)
            dependencies: list[dict] = []
            process_raw_deps(
                env_raw_deps,
                dependencies,
                deps_groups,
                project_def_path=project_def_path,
            )

            ws_editable_packages = await self.project_info_provider.get_workspace_editable_packages()
            for dep in dependencies:
                if dep["name"] in ws_editable_packages:
                    path = ws_editable_packages[dep["name"]]
                    dep["version_or_source"] = f" @ file://{path.as_posix()}"
                    dep["editable"] = True
            dependencies.extend(collect_transitive_editable_deps(dependencies, ws_editable_packages))

            overrides = payload.env.dependencies_override
            if overrides:
                self.logger.debug(f"Applying {len(overrides)} dependencies_override(s) to env '{env.name}'")
                for override_spec in overrides:
                    raw_name = get_dependency_name(override_spec.strip())
                    canonical = canonicalize_name(raw_name)
                    version_or_source = override_spec.strip()[len(raw_name):]
                    replaced = False
                    for dep in dependencies:
                        if canonicalize_name(dep["name"]) == canonical:
                            dep["name"] = raw_name
                            dep["version_or_source"] = version_or_source
                            replaced = True
                            break
                    if not replaced:
                        new_dep: dict = {
                            "name": raw_name,
                            "version_or_source": version_or_source,
                            "editable": False,
                        }
                        if raw_name in ws_editable_packages:
                            path = ws_editable_packages[raw_name]
                            new_dep["version_or_source"] = f" @ file://{path.as_posix()}"
                            new_dep["editable"] = True
                        dependencies.append(new_dep)

            install_deps_payload = install_deps_in_env_action.InstallDepsInEnvRunPayload(
                env_name=env.name,
                venv_dir_path=env.venv_dir_path,
                project_dir_path=path_to_resource_uri(project_def_path.parent),
                dependencies=[
                    install_deps_in_env_action.Dependency(
                        name=dep["name"],
                        version_or_source=dep["version_or_source"],
                        editable=dep["editable"],
                    )
                    for dep in dependencies
                ],
            )

            await progress.report("Installing dependencies")
            result = await self.action_runner.run_action(
                action_type=install_deps_in_env_action.InstallDepsInEnvAction,
                payload=install_deps_payload,
                meta=run_context.meta,
            )
            return InstallEnvsRunResult(errors=result.errors)
