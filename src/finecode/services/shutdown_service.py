from loguru import logger

from finecode import context
from finecode.runner import runner_client, runner_manager


def on_shutdown(ws_context: context.WorkspaceContext):
    running_runners = []
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status == runner_client.RunnerStatus.RUNNING:
                running_runners.append(runner)

    logger.trace(f"Stop all {len(running_runners)} running extension runners")

    for runner in running_runners:
        runner_manager.stop_extension_runner_sync(runner=runner)

    if ws_context.runner_io_thread is not None:
        logger.trace("Stop IO thread")
        ws_context.runner_io_thread.stop(timeout=5)

    # TODO: stop MCP if running
