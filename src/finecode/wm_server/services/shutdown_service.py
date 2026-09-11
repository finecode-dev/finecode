import asyncio

from loguru import logger

from finecode.wm_server import context
from finecode.wm_server.runner import runner_client, runner_manager

try:
    from finecode.wm_server.services import knowledge_service
except ImportError:
    # finecode_knowledge is the optional `finecode[knowledge]` extra; without it
    # no store was ever loaded, so there is nothing pending to flush.
    knowledge_service = None


async def on_shutdown(ws_context: context.WorkspaceContext) -> None:
    # Knowledge fact writes are throttled, so a crash between them can lose
    # the refreshes since the last one; a graceful shutdown has no reason to
    # accept even that, so it flushes unconditionally before anything else --
    # this needs no runner and nothing below it depends on runners being up.
    if knowledge_service is not None:
        await knowledge_service.persist_pending(ws_context)

    running_runners = []
    initializing_runners = []
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status in (
                runner_client.RunnerStatus.RUNNING,
                runner_client.RunnerStatus.REPAIRING,
            ):
                running_runners.append(runner)
            elif runner.status == runner_client.RunnerStatus.INITIALIZING:
                initializing_runners.append(runner)

    logger.trace(f"Stop all {len(running_runners)} running extension runners")

    # Stop them all concurrently rather than one at a time: each stop is
    # already bounded to _STOP_TIMEOUT_SEC, but this runs synchronously inside
    # the WM's own event loop (nothing else can proceed meanwhile), so doing
    # it sequentially means total shutdown time scales with runner count —
    # under a heavily loaded workspace with dozens of runners, a handful being
    # slow to confirm turns a bounded-per-runner wait into a WM that looks
    # hung for many minutes. Concurrently, total time stays ~_STOP_TIMEOUT_SEC
    # regardless of how many runners there are.
    await asyncio.gather(
        *(
            runner_manager.stop_extension_runner(
                runner=runner, ws_context=ws_context
            )
            for runner in running_runners
        )
    )

    # A runner still INITIALIZING has never been sent a shutdown/exit RPC (that
    # only happens for RUNNING/REPAIRING), so there is no graceful cleanup in
    # progress to interrupt — force-killing here is the only way to reach it,
    # since it may have an OS process spawned but no way left to reach it once
    # the WM stops (e.g. WM shutdown races with an in-flight ER start attempt).
    for runner in initializing_runners:
        if runner.client is not None:
            runner.client.force_kill()
        await ws_context.process_budget.reclaim_for_runner(runner.readable_id)

    if ws_context.runner_io_thread is not None:
        logger.trace("Stop IO thread")
        ws_context.runner_io_thread.stop(timeout=5)
