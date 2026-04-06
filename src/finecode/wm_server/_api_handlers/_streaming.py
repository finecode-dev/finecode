"""Streaming and progress-reporting action run handlers."""
from __future__ import annotations

import asyncio
import uuid

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._helpers import (
    _build_batch_result,
    _notify_client,
    _parse_and_validate_run_action_params,
    _parse_run_batch_params,
    _resolve_actions_by_project,
)
from finecode.wm_server._jsonrpc import (
    NOT_IMPLEMENTED_CODE,
    _NotImplementedError,
    _jsonrpc_error,
    _jsonrpc_response,
    _write_message,
)


async def _handle_run_with_partial_results(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
) -> dict:
    """Handle the ``actions/runWithPartialResults`` request.

    The handler uses :mod:`partial_results_service` to obtain an async iterator
    of partial values and forwards them to the requesting client only.  When the
    iterator completes an aggregated result dict is returned exactly as the
    ``actions/run`` method would produce.
    """
    if params is None:
        raise ValueError("params required")
    action_name = params.get("action")
    token = params.get("partialResultToken")
    if not action_name or token is None:
        raise ValueError("action and partial_result_token are required")
    project_path = params.get("project", "")
    options = params.get("options", {})

    from finecode.wm_server.services import partial_results_service, run_service

    trigger = run_service.RunActionTrigger(options.get("trigger", "system"))
    dev_env = run_service.DevEnv(options.get("devEnv", "ide"))
    result_formats = options.get("resultFormats", ["json"])

    logger.trace(f"runWithPartialResults: action={action_name} project={project_path!r} token={token} formats={result_formats}")

    progress_token = params.get("progressToken")

    stream = await partial_results_service.run_action_with_partial_results(
        action_name=action_name,
        project_path=project_path,
        params=params.get("params", {}),
        partial_result_token=token,
        run_trigger=trigger,
        dev_env=dev_env,
        ws_context=ws_context,
        result_formats=result_formats,
        progress_token=progress_token,
    )

    async def _forward_partials() -> int:
        count = 0
        async for value in stream:
            count += 1
            logger.trace(f"runWithPartialResults: sending partial #{count} for token={token}, keys={list(value.keys()) if isinstance(value, dict) else type(value)}")
            _notify_client(
                writer,
                "actions/partialResult",
                {"token": token, "value": value},
            )
            await writer.drain()
        return count

    async def _forward_progress() -> None:
        if stream.progress_stream is None or progress_token is None:
            return
        async for value in stream.progress_stream:
            logger.trace(f"runWithPartialResults: sending progress type={value.get('type')} for token={progress_token}")
            _notify_client(
                writer,
                "actions/progress",
                {"token": progress_token, "value": value},
            )
            await writer.drain()

    partial_count = 0
    async with asyncio.TaskGroup() as forward_tg:
        partials_task = forward_tg.create_task(_forward_partials())
        forward_tg.create_task(_forward_progress())
    partial_count = partials_task.result()

    final = await stream.final_result()
    logger.trace(f"runWithPartialResults: done, sent {partial_count} partials, final keys={list(final.keys()) if isinstance(final, dict) else type(final)}")
    return final


async def _handle_run_with_partial_results_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task to handle the ``actions/runWithPartialResults`` request asynchronously.

    This runs in a separate task to avoid blocking the client handler loop
    during long-running actions.
    """
    try:
        result = await _handle_run_with_partial_results(
            params, ws_context, writer
        )
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(
            writer,
            _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)),
        )
        await writer.drain()
    except Exception as exc:
        logger.exception(
            "FineCode API: error handling actions/runWithPartialResults"
        )
        _write_message(
            writer, _jsonrpc_error(req_id, -32603, str(exc))
        )
        await writer.drain()


async def _handle_run_action_with_progress(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
) -> dict:
    """Handle ``actions/run`` when a ``progressToken`` is present.

    Runs the action normally but concurrently listens for progress
    notifications on the runner and forwards them to the requesting client.
    """
    if params is None:
        raise ValueError("params required")

    from finecode.wm_server.services import run_service
    from finecode.wm_server.services.run_service import proxy_utils

    parsed = _parse_and_validate_run_action_params(params, ws_context)
    progress_token = params["progressToken"]

    action = next(
        (a for a in parsed.project.actions if a.name == parsed.action_name), None
    )
    if action is None:
        raise ValueError(f"Action '{parsed.action_name}' not found in project '{params.get('project')}'")

    # Ensure runners are started before subscribing to progress notifications
    # so the runner objects exist in ws_projects_extension_runners.
    await run_service.start_required_environments(
        {parsed.project.dir_path: [parsed.action_name]},
        ws_context,
        initialize_all_handlers=True,
    )

    # Subscribe to progress on all runners for this project's action handlers.
    progress_list: proxy_utils.AsyncList[domain.ProgressRawValue] = proxy_utils.AsyncList()
    progress_tasks: list[asyncio.Task] = []
    runners_by_env = ws_context.ws_projects_extension_runners.get(parsed.project.dir_path, {})
    for handler in action.handlers:
        runner = runners_by_env.get(handler.env)
        if runner is not None:
            task = asyncio.create_task(
                proxy_utils.get_progress(
                    result_list=progress_list,
                    progress_token=progress_token,
                    runner=runner,
                )
            )
            progress_tasks.append(task)

    async def _forward_progress() -> None:
        try:
            async for value in progress_list:
                logger.trace(f"runAction: sending progress type={value.get('type')} for token={progress_token}")
                _notify_client(
                    writer,
                    "actions/progress",
                    {"token": progress_token, "value": value},
                )
                await writer.drain()
        except asyncio.CancelledError:
            pass

    forward_task = asyncio.create_task(_forward_progress())

    try:
        executor = run_service.ProjectExecutor(ws_context)
        result = await executor.run_action(
            action_source=action.source,
            params=parsed.action_params,
            project_path=parsed.project.dir_path,
            run_trigger=parsed.trigger,
            dev_env=parsed.dev_env,
            result_formats=parsed.result_formats,
            initialize_all_handlers=True,
            progress_token=progress_token,
        )
        return {
            "resultByFormat": result.result_by_format,
            "returnCode": result.return_code,
        }
    finally:
        for t in progress_tasks:
            t.cancel()
        progress_list.end()
        await asyncio.gather(*progress_tasks, return_exceptions=True)
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)


async def _handle_run_action_with_progress_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task wrapper for ``actions/run`` with progress — mirrors
    ``_handle_run_with_partial_results_task``."""
    try:
        result = await _handle_run_action_with_progress(
            params, ws_context, writer
        )
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(
            writer,
            _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)),
        )
        await writer.drain()
    except Exception as exc:
        logger.exception(
            "FineCode API: error handling actions/run with progress"
        )
        _write_message(
            writer, _jsonrpc_error(req_id, -32603, str(exc))
        )
        await writer.drain()


async def _handle_run_batch_with_progress(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
) -> dict:
    """Handle ``actions/runBatch`` when a ``progressToken`` is present.

    Mirrors ``_handle_run_batch`` but subscribes to runner progress notifications,
    aggregates them via Strategy C (sum-of-totals) across all (project × action)
    slots, and forwards the combined stream to the client as ``actions/progress``
    notifications while the batch is running.
    """
    from finecode.wm_server.services import partial_results_service, run_service
    from finecode.wm_server.services.run_service import proxy_utils

    params = params or {}
    parsed = _parse_run_batch_params(params)
    progress_token: str = params["progressToken"]

    if not parsed.actions:
        raise ValueError("actions list is required and must be non-empty")

    logger.debug(f"runBatch+progress: actions={parsed.actions} projects={parsed.project_names}")

    actions_by_project = _resolve_actions_by_project(parsed.project_names, parsed.actions, ws_context)

    await run_service.start_required_environments(
        actions_by_project, ws_context, update_config_in_running_runners=True
    )

    # One aggregation slot per (project × action) pair.
    # Each slot gets a unique internal progress token sent to its ER.
    slot_count = sum(len(acts) for acts in actions_by_project.values())
    combined_stream = partial_results_service.ProgressStream()
    aggregator = partial_results_service.ProgressAggregator(slot_count, combined_stream)

    # progress_token_by_project[project_path][action_name] = internal_token
    progress_token_by_project: dict[pathlib.Path, dict[str, str]] = {}
    # slot_lists[slot_key] = AsyncList that get_progress tasks write into
    slot_lists: dict[str, proxy_utils.AsyncList[domain.ProgressRawValue]] = {}
    get_progress_tasks: list[asyncio.Task] = []

    import pathlib as pathlib_mod
    for project_path, actions_to_run in actions_by_project.items():
        project_def = ws_context.ws_projects[project_path]
        if not isinstance(project_def, domain.CollectedProject):
            continue
        progress_token_by_project[project_path] = {}
        runners_by_env = ws_context.ws_projects_extension_runners.get(project_path, {})

        for action_name in actions_to_run:
            internal_token = f"progress-{uuid.uuid4()}"
            progress_token_by_project[project_path][action_name] = internal_token
            slot_key = f"{project_def.name}/{action_name}"

            slot_list: proxy_utils.AsyncList[domain.ProgressRawValue] = proxy_utils.AsyncList()
            slot_lists[slot_key] = slot_list

            action_def = next((a for a in project_def.actions if a.name == action_name), None)
            if action_def is None:
                continue
            for handler in action_def.handlers:
                runner = runners_by_env.get(handler.env)
                if runner is not None:
                    task = asyncio.create_task(
                        proxy_utils.get_progress(
                            result_list=slot_list,
                            progress_token=internal_token,
                            runner=runner,
                        )
                    )
                    get_progress_tasks.append(task)

    # One task per slot: reads from slot_list and routes to aggregator
    aggregator_tasks: list[asyncio.Task] = []
    for slot_key, slot_list in slot_lists.items():
        async def _forward_slot(sl: proxy_utils.AsyncList = slot_list, key: str = slot_key) -> None:
            async for value in sl:
                aggregator.on_progress(key, value)
        aggregator_tasks.append(asyncio.create_task(_forward_slot()))

    # Forward combined aggregated stream to client
    async def _forward_to_client() -> None:
        try:
            async for value in combined_stream:
                logger.trace(f"runBatch+progress: forwarding type={value.get('type')} token={progress_token}")
                _notify_client(writer, "actions/progress", {"token": progress_token, "value": value})
                await writer.drain()
        except asyncio.CancelledError:
            pass

    client_forward_task = asyncio.create_task(_forward_to_client())

    try:
        workspace_executor = run_service.WorkspaceExecutor(ws_context)
        result_by_project = await workspace_executor.run_actions_in_projects(
            actions_by_project=actions_by_project,
            params=parsed.action_params,
            run_trigger=parsed.trigger,
            dev_env=parsed.dev_env,
            concurrently=parsed.concurrently,
            result_formats=parsed.result_formats,
            payload_overrides_by_project=parsed.params_by_project or None,
            progress_token_by_project=progress_token_by_project,
        )
    finally:
        # Cancel get_progress tasks first, then drain slot lists through aggregator,
        # then signal combined_stream done so client forward task can exit cleanly.
        for t in get_progress_tasks:
            t.cancel()
        await asyncio.gather(*get_progress_tasks, return_exceptions=True)
        for sl in slot_lists.values():
            sl.end()
        await asyncio.gather(*aggregator_tasks, return_exceptions=True)
        combined_stream.set_done()
        await asyncio.gather(client_forward_task, return_exceptions=True)

    results, overall_return_code = _build_batch_result(result_by_project)
    logger.debug(f"runBatch+progress: done, projects_count={len(results)} returnCode={overall_return_code}")
    return {"results": results, "returnCode": overall_return_code}


async def _handle_run_batch_with_progress_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task wrapper for ``actions/runBatch`` with progress."""
    try:
        result = await _handle_run_batch_with_progress(params, ws_context, writer)
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(writer, _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)))
        await writer.drain()
    except Exception as exc:
        logger.exception("FineCode API: error handling actions/runBatch with progress")
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
