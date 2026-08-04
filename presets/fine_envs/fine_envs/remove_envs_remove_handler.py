import dataclasses

from fine_envs import remove_envs_action
from fine_envs.create_envs_action import env_label
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifilemanager, ilogger
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class RemoveEnvsRemoveHandlerConfig(code_action.ActionHandlerConfig): ...


class RemoveEnvsRemoveHandler(
    code_action.ActionHandler[
        remove_envs_action.RemoveEnvsAction, RemoveEnvsRemoveHandlerConfig
    ]
):
    """Delete each resolved env's directory.

    Envs are removed one at a time rather than concurrently: the work is a
    filesystem walk, not a subprocess, so there is nothing to overlap, and
    serial removal keeps progress messages in a readable order.
    """

    def __init__(
        self, file_manager: ifilemanager.IFileManager, logger: ilogger.ILogger
    ) -> None:
        self.file_manager = file_manager
        self.logger = logger

    async def run(
        self,
        payload: remove_envs_action.RemoveEnvsRunPayload,
        run_context: remove_envs_action.RemoveEnvsRunContext,
    ) -> remove_envs_action.RemoveEnvsRunResult:
        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be discovered by previous `remove_envs` handlers"
            )

        removed: list[str] = []
        errors: list[str] = []

        async with run_context.progress(
            "Removing environments", total=len(run_context.envs)
        ) as progress:
            for env in run_context.envs:
                label = env_label(env)
                venv_dir_path = resource_uri_to_path(env.venv_dir_path)
                try:
                    await self.file_manager.remove_dir(venv_dir_path, tolerant=True)
                except ifilemanager.RemoveDirError as exception:
                    # One undeletable env must not hide the others: report it
                    # and keep going, same as `clean_service_logs`.
                    errors.append(f"Failed to remove {venv_dir_path}: {exception}")
                    self.logger.warning(
                        f"Failed to remove {venv_dir_path}: {exception}"
                    )
                    await progress.advance(message=f"Failed: {label}")
                    continue

                removed.append(env.name)
                self.logger.info(f"Removed env {label} ({venv_dir_path})")
                await progress.advance(message=f"Removed: {label}")

        return remove_envs_action.RemoveEnvsRunResult(removed=removed, errors=errors)
