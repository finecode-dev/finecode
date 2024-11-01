import asyncio
import concurrent.futures as futures
import threading
from dataclasses import dataclass
from pathlib import Path

import janus
from loguru import logger
from modapp.client import Client

import finecode.domain as domain
from finecode.workspace_manager.runner_client.finecode.extension_runner import (
    ExtensionRunnerService, RunActionRequest)


@dataclass
class ExtensionRunnerInfo:
    working_dir_path: Path
    process_id: int
    output_queue: janus.Queue
    stop_event: threading.Event
    started_event: asyncio.Event
    process_future: futures.Future | None
    port: int | None = None
    client: Client | None = None


async def run_action_in_runner(
    runner: ExtensionRunnerInfo, action: domain.Action, apply_on: Path | None, apply_on_text: str
) -> str | None:
    if not runner.started_event.is_set():
        await runner.started_event.wait()
    assert runner.client is not None

    try:
        result = await ExtensionRunnerService.run_action(
            channel=runner.client.channel,
            request=RunActionRequest(
                action_name=action.name,
                apply_on=apply_on.as_posix() if apply_on is not None else "",
                apply_on_text=apply_on_text,
            ),
        )
        if result is None:
            return None
        else:
            return result.result_text
    except Exception as e:
        logger.exception(e)
