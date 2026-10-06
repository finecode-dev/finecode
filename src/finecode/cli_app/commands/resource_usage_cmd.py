# docs: docs/cli.md
"""On-demand resource-usage snapshot of the running workspace server."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import typing

import click

from finecode.cli_app import resource_usage
from finecode.wm_client import (
    ApiClient,
    ApiError,
    ApiMethodNotFoundError,
    ApiServerError,
)
from finecode.wm_server import wm_lifecycle

SHOW_TIMEOUT_SEC = 30.0


class ResourceUsageFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _default_emit_status(line: str) -> None:
    click.echo(line, err=True)


async def show(
    workdir_path: pathlib.Path,
    *,
    as_json: bool,
    watch_sec: float | None,
    include_processes: bool,
    emit: typing.Callable[[str], None] = click.echo,
    emit_status: typing.Callable[[str], None] = _default_emit_status,
) -> None:
    port = await asyncio.to_thread(wm_lifecycle.running_port)
    if port is None:
        raise ResourceUsageFailed(
            "No FineCode workspace server is running, so there is nothing to "
            "report. Start one — an editor with the FineCode LSP, the MCP "
            "server, or 'python -m finecode start-wm-server' — and run this "
            "again."
        )
    client = ApiClient()
    try:
        try:
            opening = _opening(
                client,
                port,
                as_json=as_json,
                watch_sec=watch_sec,
                include_processes=include_processes,
                emit=emit,
                emit_status=emit_status,
            )
            await asyncio.wait_for(opening, SHOW_TIMEOUT_SEC)
        except TimeoutError:
            log_path = client.server_info.get("logFilePath") or "unknown"
            raise ResourceUsageFailed(
                f"the workspace server did not answer within {SHOW_TIMEOUT_SEC:g}s "
                "— it is likely starved; its log is "
                f"{log_path}"
            ) from None
    finally:
        await client.close()


async def _opening(
    client: ApiClient,
    port: int,
    *,
    as_json: bool,
    watch_sec: float | None,
    include_processes: bool,
    emit: typing.Callable[[str], None],
    emit_status: typing.Callable[[str], None],
) -> None:
    try:
        await client.connect("127.0.0.1", port)
        info = await client.get_info()
        lag_window = (
            resource_usage.lag_window_for(watch_sec) if watch_sec is not None else None
        )
        first = await client.get_resource_usage(
            include_processes=include_processes,
            lag_window_sec=lag_window,
        )
    except ApiServerError as exc:
        raise ResourceUsageFailed(
            f"the workspace server answered with an error: {exc}"
        ) from exc
    except ApiError as exc:
        raise ResourceUsageFailed(
            f"the workspace server answered with an error: {exc}"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise ResourceUsageFailed(
            f"lost the connection to the workspace server: {exc}"
        ) from exc

    if as_json:
        emit(json.dumps(first, indent=2))
    else:
        emit(resource_usage.format_table(info, first))
    if watch_sec is None:
        return

    window = resource_usage.lag_window_for(watch_sec)

    async def _poll() -> dict:
        return await client.get_resource_usage(
            include_processes=include_processes,
            lag_window_sec=window,
        )

    def _render(snapshot: dict, _elapsed: float) -> str:
        if as_json:
            return json.dumps(snapshot)
        return resource_usage.format_table(info, snapshot)

    async with resource_usage.periodic(
        _poll,
        watch_sec,
        emit=emit,
        emit_status=emit_status,
        render=_render,
        summary=False,
        stop_on_disconnect=True,
    ) as reporter:
        await reporter.stopped.wait()

    if reporter.stop_reason == "unsupported":
        raise ResourceUsageFailed(
            "the running workspace server does not support "
            "server/getResourceUsage — it predates this CLI; replace it "
            "with `restart-wm --shared-server`"
        )
    if reporter.stop_reason == "disconnected":
        raise ResourceUsageFailed("the workspace server went away")
    if reporter.stop_reason == "failed":
        raise ResourceUsageFailed(f"the resource reporter failed: {reporter.error}")
    if reporter.stop_reason == "output_closed":
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115, ASYNC230
