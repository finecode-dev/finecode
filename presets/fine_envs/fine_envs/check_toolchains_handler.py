# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from fine_envs import check_toolchains_action, sync_toolchains_action


@dataclasses.dataclass
class CheckToolchainsHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckToolchainsHandler(
    code_action.ActionHandler[
        check_toolchains_action.CheckToolchainsAction, CheckToolchainsHandlerConfig
    ]
):
    """Re-derive the toolchain axes without writing, and report the ones that drifted."""

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self.action_runner = action_runner

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
        return check_toolchains_action.CheckToolchainsRunResult(stale_axes=stale_axes)
