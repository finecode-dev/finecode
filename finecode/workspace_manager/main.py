from __future__ import annotations

import asyncio
import concurrent.futures as futures
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import command_runner
import janus
from loguru import logger
from modapp.client import Client

# import finecode.api as api
import finecode.domain as domain
import finecode.extension_runner.schemas as schemas
import finecode.workspace_context as workspace_context
import finecode.utils.finecode_cmd as finecode_cmd
from finecode.workspace_manager.runner_client import create_client
from finecode.workspace_manager.runner_client.finecode.extension_runner import \
    ExtensionRunnerService
from finecode.workspace_manager.server.main import create_manager_app
from finecode.workspace_manager.server.api_routes import ws_context as global_ws_context

if TYPE_CHECKING:
    import subprocess


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    process_id: int
    output_queue: janus.Queue
    stop_event: threading.Event
    process_future: futures.Future | None
    port: int | None = None
    client: Client | None = None


async def start() -> None: # ws_context: workspace_context.WorkspaceContext
    manager_app = create_manager_app()
    await manager_app.run_async()  # TODO: stop
    await start_in_ws_context(global_ws_context)


async def start_in_ws_context(ws_context: workspace_context.WorkspaceContext) -> None:
    extension_runners_start_coros = [
        start_extension_runner(runner_dir=package_path)
        for package_path in ws_context.ws_packages
    ]
    extension_runners = await asyncio.gather(*extension_runners_start_coros)

    # one for all, doesn't need to change on ws dirs change
    asyncio.create_task(
        handle_runners_lifecycle(extension_runners, ws_context)
    )

async def handle_runners_lifecycle(
    extension_runners: list[ExtensionRunnerInfo], ws_context: workspace_context.WorkspaceContext
):
    ws_dirs = ws_context.ws_dirs_paths.copy()
    try:
        while True:
            await ws_context.ws_dirs_paths_changed.wait()
            new_dirs, deleted_dirs = _find_changed_dirs(ws_context.ws_dirs_paths, ws_dirs)
            for deleted_dir in deleted_dirs:
                try:
                    runner_to_delete = next(runner for runner in extension_runners if runner.working_dir_path == deleted_dir)
                except StopIteration:
                    continue
                stop_extension_runner(runner_to_delete)
                extension_runners.remove(runner_to_delete)

            new_runners_coros = [start_extension_runner(runner_dir=new_dir) for new_dir in new_dirs]
            new_runners = await asyncio.gather(*new_runners_coros)
            extension_runners += new_runners
    finally:
        # TODO: stop all log handlers?
        for runner in extension_runners:
            stop_extension_runner(runner)


def _find_changed_dirs(new_dirs: list[Path], old_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    added_dirs: list[Path] = []
    deleted_dirs: list[Path] = []
    for new_dir in new_dirs:
        if new_dir not in old_dirs:
            added_dirs.append(new_dir)
    for old_dir in old_dirs:
        if old_dir not in new_dirs:
            deleted_dirs.append(old_dir)

    return added_dirs, old_dirs


async def start_extension_runner(
    runner_dir: Path
) -> ExtensionRunnerInfo:
    runner_info = ExtensionRunnerInfo(
        process_id=0,
        working_dir_path=runner_dir,
        output_queue=janus.Queue(),
        process_future=None,
        stop_event=threading.Event()
    )

    def save_process_info(runner_info: ExtensionRunnerInfo, process: subprocess.Popen):
        runner_info.process_id = process.pid
        logger.trace(
            f"Started extension runner in {runner_info.working_dir_path}, pid {runner_info.process_id}"
        )

    def should_stop(stop_event: threading.Event) -> bool:
        return stop_event.is_set()

    _finecode_cmd = finecode_cmd.get_finecode_cmd(runner_dir)
    runner_info.process_future = command_runner.command_runner_threaded(
        f"{_finecode_cmd} runner",
        process_callback=partial(save_process_info, runner_info),
        stdout=runner_info.output_queue.sync_q,
        # method poller is required to get stdout in threaded mode
        method="poller",
        stop_on=partial(should_stop, runner_info.stop_event),
    )  # type: ignore

    asyncio.create_task(extension_runner_log_handler(runner_info))
    return runner_info


def stop_extension_runner(runner: ExtensionRunnerInfo) -> None:
    runner.stop_event.set()
    if runner.process_future is not None:
        # wait for end of the process to make sure it was stopped
        runner.process_future.result()
    logger.trace(f"Stop extension runner {runner.process_id} in {runner.working_dir_path}")


async def extension_runner_log_handler(runner: ExtensionRunnerInfo) -> None:
    read_queue = True
    while read_queue:
        line = await runner.output_queue.async_q.get()  # timeout=0.1
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


async def run_action_in_runner(runner: ExtensionRunnerInfo, action: domain.Action, apply_on: Path):
    try:
        await ExtensionRunnerService.run_action(
            channel=runner.client.channel,
            request=schemas.RunActionRequest(
                action_name=action.name, apply_on=apply_on.as_posix()
            ),
        )
    except Exception as e:
        logger.exception(e)
