from __future__ import annotations
import asyncio
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
import threading
import concurrent.futures as futures

import command_runner
import janus
from loguru import logger
from modapp.client import Client

import finecode.workspace_context as workspace_context
import finecode.api as api
import finecode.domain as domain
import finecode.extension_runner.schemas as schemas
from finecode.workspace_manager.runner_client.finecode.extension_runner import (
    ExtensionRunnerService,
)
from finecode.workspace_manager.runner_client import create_client

if TYPE_CHECKING:
    import subprocess


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    process_id: int
    output_queue: janus.Queue
    process_future: futures.Future | None
    port: int | None = None
    client: Client | None = None


async def start(ws_context: workspace_context.WorkspaceContext) -> None:
    api.read_configs(ws_context=ws_context)

    await start_in_ws_context(ws_context)


async def start_in_ws_context(ws_context: workspace_context.WorkspaceContext) -> None:
    # TODO: adapt runners if packages are changed
    stop_event = threading.Event()
    extension_runners_start_coros = [
        start_extension_runner(runner_dir=package_path, stop_event=stop_event)
        for package_path in ws_context.ws_packages
    ]
    extension_runners = await asyncio.gather(*extension_runners_start_coros)
    log_handlers_tasks = [
        asyncio.create_task(extension_runner_log_handler(runner))
        for runner in extension_runners
    ]
    runners_lifecycle_task = asyncio.create_task(
        handle_runners_lifecycle(extension_runners, stop_event)
    )


async def handle_runners_lifecycle(
    extension_runners: list[ExtensionRunnerInfo], stop_event: threading.Event
):
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        stop_event.set()
        # TODO: stop all log handlers?
        for runner in extension_runners:
            # os.kill(runner.process_id, signal.SIGINT)
            if runner.process_future is not None:
                # wait for end of the process to make sure it was stopped
                runner.process_future.result()
            logger.trace(
                f"Stop extension runner {runner.process_id} in {runner.working_dir_path}"
            )


async def start_extension_runner(
    runner_dir: Path, stop_event: threading.Event
) -> ExtensionRunnerInfo:
    def save_process_info(runner_info: ExtensionRunnerInfo, process: subprocess.Popen):
        runner_info.process_id = process.pid
        logger.trace(
            f"Started extension runner in {runner_info.working_dir_path}, pid {runner_info.process_id}"
        )

    def should_stop(stop_event: threading.Event) -> bool:
        return stop_event.is_set()

    runner_info = ExtensionRunnerInfo(
        process_id=0,
        working_dir_path=runner_dir,
        output_queue=janus.Queue(),
        process_future=None,
    )
    runner_info.process_future = command_runner.command_runner_threaded(
        "poetry run finecode runner",
        process_callback=partial(save_process_info, runner_info),
        stdout=runner_info.output_queue.sync_q,
        # method poller is required to get stdout in threaded mode
        method="poller",
        stop_on=partial(should_stop, stop_event),
    )  # type: ignore
    return runner_info


async def extension_runner_log_handler(runner: ExtensionRunnerInfo) -> None:
    read_queue = True
    while read_queue:
        line = await runner.output_queue.async_q.get() # timeout=0.1
        if line is None:
            read_queue = False
        else:
            print(line)
            if runner.port is None and "Start web socketify server: " in line:
                runner.port = int(line.split(":")[-1])
                runner.client = create_client(f"http://localhost:{runner.port}")
                try:
                    await ExtensionRunnerService.update_config(
                        channel=runner.client.channel,
                        request=schemas.UpdateConfigRequest(
                            working_dir=runner.working_dir_path.as_posix(),
                            config={},
                        ),
                    )
                except Exception as e:
                    logger.exception(e)

    logger.trace(f"End log handler for {runner.working_dir_path}({runner.process_id})")


async def run_action_in_runner(
    runner: ExtensionRunnerInfo, action: domain.Action, apply_on: Path
):
    try:
        await ExtensionRunnerService.run_action(
            channel=runner.client.channel,
            request=schemas.RunActionRequest(
                action_name=action.name, apply_on=apply_on.as_posix()
            ),
        )
    except Exception as e:
        logger.exception(e)
