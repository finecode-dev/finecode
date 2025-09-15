from loguru import logger

from finecode import context
from finecode.runner import manager as runner_manager
from finecode.runner import runner_info


def on_shutdown(ws_context: context.WorkspaceContext):
    running_runners = []
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status == runner_info.RunnerStatus.RUNNING:
                running_runners.append(runner)

    logger.trace(f"Stop all {len(running_runners)} running extension runners")

    for runner in running_runners:
        runner_manager.stop_extension_runner_sync(runner=runner)

    # TODO: stop MCP if running
