# docs: docs/reference/actions.md
import dataclasses

from fine_envs import check_toolchains_action, sync_toolchains_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner, iprojectinfoprovider
from finecode_extension_api.resource_uri import path_to_resource_uri


@dataclasses.dataclass
class CheckToolchainsHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckToolchainsHandler(
    code_action.ActionHandler[
        check_toolchains_action.CheckToolchainsAction, CheckToolchainsHandlerConfig
    ]
):
    """Re-derive the toolchain axes without writing, and report the ones that drifted."""

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: check_toolchains_action.CheckToolchainsRunPayload,
        run_context: check_toolchains_action.CheckToolchainsRunContext,
    ) -> check_toolchains_action.CheckToolchainsRunResult:
        sync_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                sync_toolchains_action.SyncToolchainsAction
            ),
            payload=sync_toolchains_action.SyncToolchainsRunPayload(
                project_def_path=payload.project_def_path,
                save=False,
            ),
            meta=run_context.meta,
        )
        stale_axes = [axis for axis in sync_result.axes if axis.changed]
        # Report the file the axes were read from, not just that they drifted: the
        # handler runs in the project's own ER, so this is the only place that knows
        # it without a second lookup.
        project_def_path = payload.project_def_path or path_to_resource_uri(
            self.project_info_provider.get_current_project_def_path()
        )
        return check_toolchains_action.CheckToolchainsRunResult(
            stale_axes=stale_axes, project_def_path=project_def_path
        )
