import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
    iuser_messenger,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_envs import env_inventory, remove_envs_action
from fine_envs.create_envs_action import EnvInfo


@dataclasses.dataclass
class RemoveEnvsDiscoverEnvsHandlerConfig(code_action.ActionHandlerConfig): ...


class RemoveEnvsDiscoverEnvsHandler(
    code_action.ActionHandler[
        remove_envs_action.RemoveEnvsAction, RemoveEnvsDiscoverEnvsHandlerConfig
    ]
):
    """Resolve which envs to remove and refuse the ones that must not be.

    ``payload.env_names is None`` means discover, which resolves to the
    project's orphaned envs. A list is taken literally and checked against two
    guards before it reaches the removal handler.
    """

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        user_messenger: iuser_messenger.IUserMessenger,
        logger: ilogger.ILogger,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.runner_info_provider = runner_info_provider
        self.user_messenger = user_messenger
        self.logger = logger

    async def run(
        self,
        payload: remove_envs_action.RemoveEnvsRunPayload,
        run_context: remove_envs_action.RemoveEnvsRunContext,
    ) -> remove_envs_action.RemoveEnvsRunResult:
        project_def_path = self.project_info_provider.get_current_project_def_path()
        project_raw_config = (
            await self.project_info_provider.get_current_project_raw_config()
        )
        declared_names = set(project_raw_config.get("dependency-groups", {}))

        venvs_dir_path = self.runner_info_provider.get_current_venv_dir_path().parent
        existing = env_inventory.read_existing_envs(venvs_dir_path)
        current_env_name = self.runner_info_provider.get_current_env_name()

        if payload.env_names is None:
            # The current env is never a candidate for auto-discovery, even if
            # it happens to be orphaned (e.g. renamed away in config without an
            # ER restart) — same "never removable" rule `_check_guards` applies
            # to an explicitly-named target, just applied silently here since
            # discovery never asked for this env by name.
            envs = env_inventory.scan_envs(
                declared_names=declared_names,
                venvs_dir_path=venvs_dir_path,
                existing=existing,
            )
            target_names = [
                env.name
                for env in envs
                if env.orphaned and env.name != current_env_name
            ]
        else:
            target_names = list(payload.env_names)
            self._check_guards(
                target_names=target_names,
                declared_names=declared_names,
                current_env_name=current_env_name,
                force=payload.force,
            )
            target_names = self._drop_absent(
                target_names=target_names, existing=existing, run_context=run_context
            )

        run_context.envs = [
            EnvInfo(
                name=name,
                venv_dir_path=path_to_resource_uri(venvs_dir_path / name),
                project_def_path=path_to_resource_uri(project_def_path),
            )
            for name in target_names
        ]
        self.logger.debug(f"Envs to remove: {target_names}")

        return remove_envs_action.RemoveEnvsRunResult()

    def _check_guards(
        self,
        target_names: list[str],
        declared_names: set[str],
        current_env_name: str,
        force: bool,
    ) -> None:
        """Guards apply to what was *asked for*, before existence is checked —
        a rejection must not depend on whether the venv happens to be on disk
        right now."""
        if current_env_name in target_names:
            raise code_action.ActionFailedException(
                f"Cannot remove env '{current_env_name}': it is the environment this"
                " handler is running in. Removing it would break the Extension Runner"
                " performing the removal."
            )

        if force:
            return

        declared_targets = sorted(set(target_names) & declared_names)
        if declared_targets:
            raise code_action.ActionFailedException(
                f"Refusing to remove env(s) still declared in configuration:"
                f" {', '.join(declared_targets)}. An Extension Runner may be running"
                " in them. Pass force=true to remove anyway, then run prepare-envs to"
                " recreate them."
            )

    def _drop_absent(
        self,
        target_names: list[str],
        existing: dict[str, env_inventory.EnvState],
        run_context: remove_envs_action.RemoveEnvsRunContext,
    ) -> list[str]:
        absent = [name for name in target_names if name not in existing]
        if absent:
            message = (
                f"No environment on disk for: {', '.join(absent)}. Nothing to remove"
                " for those names."
            )
            if run_context.meta.trigger == code_action.RunActionTrigger.USER:
                self.user_messenger.warning(message)
            else:
                self.logger.debug(message)

        return [name for name in target_names if name in existing]
