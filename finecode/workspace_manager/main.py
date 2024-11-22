from __future__ import annotations

import asyncio
import os
import threading
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import command_runner
import janus
from loguru import logger

import finecode.domain as domain
import finecode.workspace_context as workspace_context
import finecode.workspace_manager.api as manager_api
import finecode.workspace_manager.finecode_cmd as finecode_cmd
from finecode.workspace_manager.runner_client import create_client
from finecode.workspace_manager.runner_client.finecode.extension_runner import (
    ExtensionRunnerService, UpdateConfigRequest)
from finecode.workspace_manager.runner_client.modapp import ModappService, DataclassModel
from finecode.workspace_manager.server.api_routes import \
    ws_context as global_ws_context
from finecode.workspace_manager.server.main import create_manager_app
from modapp.extras.logs import save_logs_to_file
from modapp.extras.platformdirs import get_dirs

if TYPE_CHECKING:
    import subprocess


async def start() -> None:
    log_dir_path = Path(get_dirs(app_name='FineCode_Workspace_Manager', app_author='FineCode', version='1.0').user_log_dir)
    # tmp until fixed in modapp
    logger.remove()
    save_logs_to_file(file_path=log_dir_path / 'execution.log', log_level="TRACE")
    manager_app = create_manager_app()
    await manager_app.run_async()  # TODO: stop
    await start_in_ws_context(global_ws_context)


async def start_in_ws_context(ws_context: workspace_context.WorkspaceContext) -> None:
    # one for all, doesn't need to change on ws dirs change
    asyncio.create_task(handle_runners_lifecycle(ws_context))


async def update_runners(ws_context: workspace_context.WorkspaceContext) -> None:
    extension_runners = list(ws_context.ws_packages_extension_runners.values())
    new_dirs, deleted_dirs = _find_changed_dirs(
        [*ws_context.ws_packages.keys()], [runner.working_dir_path for runner in extension_runners]
    )
    for deleted_dir in deleted_dirs:
        try:
            runner_to_delete = next(
                runner for runner in extension_runners if runner.working_dir_path == deleted_dir
            )
        except StopIteration:
            continue
        stop_extension_runner(runner_to_delete)
        extension_runners.remove(runner_to_delete)

    new_runners_coros = [
        start_extension_runner(runner_dir=new_dir, ws_context=ws_context) for new_dir in new_dirs
    ]
    new_runners = await asyncio.gather(*new_runners_coros)
    extension_runners += [runner for runner in new_runners if runner is not None]

    ws_context.ws_packages_extension_runners = {
        runner.working_dir_path: runner for runner in extension_runners
    }


async def handle_runners_lifecycle(ws_context: workspace_context.WorkspaceContext):
    await update_runners(ws_context)
    try:
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(e)
    finally:
        # TODO: stop all log handlers?
        for runner in ws_context.ws_packages_extension_runners.values():
            stop_extension_runner(runner)

        ws_context.ws_packages_extension_runners.clear()


def _find_changed_dirs(
    new_dirs: Sequence[Path], old_dirs: Sequence[Path]
) -> tuple[list[Path], list[Path]]:
    added_dirs: list[Path] = []
    deleted_dirs: list[Path] = []
    for new_dir in new_dirs:
        if new_dir not in old_dirs:
            added_dirs.append(new_dir)
    for old_dir in old_dirs:
        if old_dir not in new_dirs:
            deleted_dirs.append(old_dir)

    return added_dirs, deleted_dirs


async def start_extension_runner(
    runner_dir: Path, ws_context: workspace_context.WorkspaceContext
) -> manager_api.ExtensionRunnerInfo | None:
    runner_info = manager_api.ExtensionRunnerInfo(
        process_id=0,
        working_dir_path=runner_dir,
        output_queue=janus.Queue(),
        started_event=asyncio.Event(),
        process_future=None,
        stop_event=threading.Event(),
    )

    def save_process_info(runner_info: manager_api.ExtensionRunnerInfo, process: subprocess.Popen):
        runner_info.process_id = process.pid
        logger.trace(
            f"Started extension runner in {runner_info.working_dir_path}, pid {runner_info.process_id}"
        )

    def should_stop(stop_event: threading.Event) -> bool:
        return stop_event.is_set()

    try:
        _finecode_cmd = finecode_cmd.get_finecode_cmd(runner_dir)
    except ValueError:
        try:
            ws_context.ws_packages[runner_dir].status = domain.PackageStatus.NO_FINECODE_SH
        except KeyError:
            ...
        return None

    # temporary remove VIRTUAL_ENV env variable to avoid starting in wrong venv
    old_virtual_env_var = os.environ.get("VIRTUAL_ENV", "")
    os.environ["VIRTUAL_ENV"] = ""
    runner_info.process_future = command_runner.command_runner_threaded(
        f"{_finecode_cmd} runner",
        process_callback=partial(save_process_info, runner_info),
        stdout=runner_info.output_queue.sync_q,
        # method poller is required to get stdout in threaded mode
        method="poller",
        stop_on=partial(should_stop, runner_info.stop_event),
        timeout=None,
    )  # type: ignore
    os.environ["VIRTUAL_ENV"] = old_virtual_env_var

    asyncio.create_task(extension_runner_log_handler(runner_info))
    return runner_info


def stop_extension_runner(runner: manager_api.ExtensionRunnerInfo) -> None:
    runner.stop_event.set()
    if runner.process_future is not None:
        # wait for end of the process to make sure it was stopped
        runner.process_future.result()
    logger.trace(f"Stop extension runner {runner.process_id} in {runner.working_dir_path}")


async def extension_runner_log_handler(runner: manager_api.ExtensionRunnerInfo) -> None:
    read_queue = True
    while read_queue:
        line = await runner.output_queue.async_q.get()  # timeout=0.1
        if line is None:
            read_queue = False
        else:
            print(f"R{runner.process_id}", line)
            if runner.port is None and "Start server: " in line:
                runner.port = int(line.split(":")[-1])
                runner.client = create_client(f"http://localhost:{runner.port}")
                try:
                    await ExtensionRunnerService.update_config(
                        channel=runner.client.channel,
                        request=UpdateConfigRequest(
                            working_dir=runner.working_dir_path.as_posix(),
                            config={},  # TODO: config
                        ),
                        # on update_config extension runner also reads all configs, it can take a
                        # time
                        timeout=100,
                    )
                    runner.started_event.set()
                    asyncio.create_task(keep_running_until_disconnect(runner))
                except Exception as e:
                    # TODO: set package status to appropriate error
                    logger.exception(e)

    logger.trace(f"End log handler for {runner.working_dir_path}({runner.process_id})")


async def keep_running_until_disconnect(runner: manager_api.ExtensionRunnerInfo):
    logger.trace('Request keep running until disconnect')
    stream = await ModappService.keep_running_until_disconnect(channel=runner.client.channel, request=DataclassModel())
    async for _ in stream:
        ...
