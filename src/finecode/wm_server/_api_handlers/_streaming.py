"""Streaming and progress-reporting action run handlers."""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import typing
import uuid

from loguru import logger

from finecode import telemetry
from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._helpers import (
    _build_batch_result,
    _notify_client,
    _parse_and_validate_run_action_params,
    _parse_run_batch_params,
    _resolve_actions_by_project,
)
from finecode.wm_server._api_handlers._run_failure import client_message
from finecode.wm_server._jsonrpc import (
    NOT_IMPLEMENTED_CODE,
    _jsonrpc_error,
    _jsonrpc_response,
    _NotImplementedError,
    _write_message,
)
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_client,
)
from finecode.wm_server.services.run_service.exceptions import (
    ActionNotFoundError,
    ActionRunFailed,
    StartingEnvironmentsFailed,
)
from finecode.wm_server.services.run_service.execution_scopes import (
    DEFAULT_ORCHESTRATION_POLICY,
    OrchestrationPolicy,
)
from finecode.wm_server.services.run_service.merge_helpers import (
    merge_slot_results,
    strip_unrequested_formats,
)
from finecode.wm_server.utils import stream_completion


class BatchSlotResult(typing.TypedDict):
    resultByFormat: _internal_client_types.ResultByFormat
    returnCode: int


async def _handle_run_action_with_partial_results(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
) -> dict:
    """Handle ``actions/run`` when a ``partialResultToken`` is present.

    The handler uses :mod:`partial_results_service` to obtain an async iterator
    of partial values and forwards them to the requesting client only.  When the
    iterator completes the final response contains only ``returnCode``.
    An optional ``progressToken`` is also supported: progress notifications are
    forwarded concurrently with partials.
    """
    if params is None:
        raise ValueError("params required")
    with telemetry.attach_incoming_traceparent(params):
        from finecode.wm_server.config import env_selection
        from finecode.wm_server.services import (
            partial_results_service,
            run_service,
        )
        from finecode.wm_server.services.run_service import (
            run_selection,
        )
        from finecode.wm_server.services.run_service.exceptions import (
            ActionRunFailed,
        )

        action_source = params.get("actionSource")
        token = params.get("partialResultToken")
        project_path = params.get("project")
        if not action_source or token is None or project_path is None:
            raise ValueError(
                "actionSource, partialResultToken and project are required"
            )
        # `.get("options", {})` only applies its default when the key is absent
        # — a caller sending the key with an explicit `None` would pass that
        # through as-is.
        options = params.get("options") or {}

        trigger = run_service.RunActionTrigger(options.get("trigger", "system"))
        dev_env = run_service.DevEnv(options.get("devEnv", "ide"))
        result_formats = options.get("resultFormats", ["json"])

        logger.trace(
            f"run+partialResults: actionSource={action_source} project={project_path!r} token={token} formats={result_formats}"
        )

        # Resolve the action source to an action name for the internal pipeline.
        action_name = await _resolve_source_to_name(
            action_source, project_path, ws_context, run_options=options
        )

        progress_token = params.get("progressToken")

        # PRD-0003 AC8: only resolvable against a single, concrete
        # project — `project_path == ""` means "run in every project that
        # declares the action" (see `_resolve_source_to_name`), which has no
        # single env universe to resolve selectors against, so the full axis
        # runs unrestricted in that case.
        selected_envs: set[str] | None = None
        if project_path:
            run_selection.validate_run_selectors(
                options.get("interpreterSelectors", []),
                [pathlib.Path(project_path)],
                ws_context,
            )
            try:
                selected_envs = run_selection.selected_envs_for_project(
                    pathlib.Path(project_path),
                    options.get("interpreterSelectors", []),
                    dev_env.value,
                    ws_context,
                )
            except env_selection.EnvSelectionError as exc:
                raise ActionRunFailed(str(exc)) from exc
            await run_selection.check_variant_selection(
                {pathlib.Path(project_path): [action_name]},
                {pathlib.Path(project_path): selected_envs},
                options.get("interpreterSelectors", []),
                ws_context,
            )
        # else: project_path == "" ("all projects") has no single env universe to
        # validate selectors against, so validation is skipped and the full axis
        # runs unrestricted (mirrors the selected_envs skip above).

        # From here to the final result, this connection is the origin of the
        # run: an ER that asks a question mid-run is asking the client that
        # started it and nobody else (ADR-0082 rule 1). Constructed on the
        # streamed paths, which are the ones that still hold their caller's
        # connection; the dispatch below binds it to the run id it mints,
        # which is what the ER names when it asks.
        origin = elicitation_bridge.RunDispatchOrigin(
            connection=writer, request_task=asyncio.current_task()
        )
        # The service context is entered before the sinks start: production
        # runs concurrently with forwarding, and a failed sink exits the
        # context — aborting the producer — instead of waiting for a runner
        # response. Value iteration acknowledges each fence only after the
        # notify + drain handoff below resumes.
        async with partial_results_service.run_action_with_partial_results(
            action_name=action_name,
            project_path=project_path,
            params=params.get("params", {}),
            partial_result_token=token,
            run_trigger=trigger,
            dev_env=dev_env,
            ws_context=ws_context,
            result_formats=result_formats,
            progress_token=progress_token,
            selected_envs=selected_envs,
            origin=origin,
        ) as stream:
            # Opt-in (collect-style callers like MCP): accumulate the `json` format of
            # each partial per project so it can be type-safely merged into the response.
            merge_results_enabled = options.get("mergeResults", False)
            json_by_project: dict[str, list[dict]] = {}

            async def _forward_partials() -> int:
                count = 0
                async for value in stream:
                    count += 1
                    if merge_results_enabled and isinstance(value, dict):
                        project_str = value.get("project", "")
                        result_by_format = value.get("resultByFormat") or {}
                        json_by_project.setdefault(project_str, []).append(
                            result_by_format.get("json")
                        )
                    logger.trace(
                        f"run+partialResults: sending partial #{count} for token={token}, keys={list(value.keys()) if isinstance(value, dict) else type(value)}"
                    )
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
                    logger.trace(
                        f"run+partialResults: sending progress type={value.get('type')} for token={progress_token}"
                    )
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

        if merge_results_enabled and json_by_project:
            return_code = final.get("returnCode", 0) if isinstance(final, dict) else 0
            results: dict[str, dict] = {}
            for project_str, payloads in json_by_project.items():
                merged_result = await merge_slot_results(
                    project_path=pathlib.Path(project_str),
                    action_name=action_name,
                    entries=[{"json": payload} for payload in payloads],
                    requested_formats=[runner_client.RunResultFormat.JSON],
                    ws_context=ws_context,
                )
                if merged_result is not None:
                    results[project_str] = {
                        action_source: {
                            "resultByFormat": merged_result,
                            "returnCode": return_code,
                        }
                    }
            if results:
                final = {**final, "results": results}

        logger.trace(
            f"run+partialResults: done, sent {partial_count} partials, final keys={list(final.keys()) if isinstance(final, dict) else type(final)}"
        )
        return final


async def _handle_run_action_with_partial_results_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task wrapper for ``actions/run`` with ``partialResultToken``.

    Runs in a separate task to avoid blocking the client handler loop during
    long-running actions.
    """
    try:
        result = await _handle_run_action_with_partial_results(
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
    except ActionNotFoundError as exc:
        logger.error(
            "FineCode API: error handling actions/run with partialResultToken: {}", exc
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except ActionRunFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/run with partialResultToken",
            str(exc),
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except StartingEnvironmentsFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/run with partialResultToken",
            exc.message,
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except Exception as exc:
        logger.exception(
            "FineCode API: error handling actions/run with partialResultToken"
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()


async def _handle_run_batch_with_partial_results(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    orchestration_depth: int = 0,
    policy: OrchestrationPolicy = DEFAULT_ORCHESTRATION_POLICY,
) -> dict:
    """Handle ``actions/runBatch`` when a ``partialResultToken`` is present.

    Runs each action per project via the ER streaming path so that partial
    results are forwarded to the CLI as they arrive rather than waiting for
    the entire action to finish.  Emits ``actions/partialResult`` notifications
    as ER partials are received.  Each notification value::

        {
            "project": "/abs/path",
            "results": {action_source: {"resultByFormat": ..., "returnCode": int}},
            "returnCode": int
        }

    The final response contains only overall ``returnCode`` once every project
    has completed.
    """
    from finecode.wm_server.config import env_selection
    from finecode.wm_server.services import (
        project_resolution_service,
        run_service,
    )
    from finecode.wm_server.services.run_service import (
        matrix_runner,
        matrix_streaming,
        proxy_utils,
        run_selection,
    )
    from finecode.wm_server.services.run_service.exceptions import (
        ActionRunFailed,
    )

    params = params or {}
    # The connection that asked for the batch is the origin of every run in it,
    # including the ones the per-project tasks below dispatch: `origin` is
    # captured in `_stream_action`'s closure, so a task created from it carries
    # the same descriptor to every dispatch it makes (ADR-0082 rule 1).
    origin = elicitation_bridge.RunDispatchOrigin(
        connection=writer, request_task=asyncio.current_task()
    )
    with telemetry.attach_incoming_traceparent(params):
        parsed = _parse_run_batch_params(params)
        token = params["partialResultToken"]

        if not parsed.action_sources:
            raise ValueError("actionSources list is required and must be non-empty")

        logger.debug(
            f"runBatch+partialResults: actionSources={parsed.action_sources} projects={parsed.project_names}"
        )

        try:
            actions_by_project, name_to_source = await _resolve_actions_by_project(
                parsed.project_names,
                parsed.action_sources,
                ws_context,
                run_options=params.get("options") or {},
            )
        except project_resolution_service.ProjectResolutionFailed as exc:
            raise ActionRunFailed(exc.message) from exc

        run_selection.validate_run_selectors(
            parsed.interpreter_selectors,
            list(actions_by_project.keys()),
            ws_context,
        )

        # Recursion is bounded by height, not width. A nested fan-out's width can
        # never exceed the workspace's project count (er_dispatch validates every
        # requested path), so a width cap measures the workspace, not a runaway:
        # it could never fire at <= cap projects and refused every legitimate
        # workspace-wide gather above it (ADR-0095). Subprocess width is bounded
        # at the leaf by the process budget (ADR-0090).
        if orchestration_depth >= policy.max_recursion_depth:
            raise ActionRunFailed(
                f"Orchestration depth {orchestration_depth} reached limit "
                f"{policy.max_recursion_depth}. Actions: "
                f"{sorted({a for names in actions_by_project.values() for a in names})}"
            )

        try:
            selection_by_project = run_selection.selection_for_matrixed_actions(
                actions_by_project,
                parsed.interpreter_selectors,
                parsed.dev_env.value,
                ws_context,
            )
        except env_selection.EnvSelectionError as exc:
            raise ActionRunFailed(str(exc)) from exc

        await run_selection.check_variant_selection(
            actions_by_project,
            selection_by_project,
            parsed.interpreter_selectors,
            ws_context,
        )

        await run_service.start_required_environments(
            actions_by_project,
            ws_context,
            selected_envs_by_project=selection_by_project,
        )

        payload_overrides = parsed.params_by_project or {}
        # Lock to prevent concurrent writes to the shared writer from project tasks.
        writer_lock = asyncio.Lock()

        # When mergeResults is requested, collect the type-safely merged result per
        # project/action so the final response carries complete data for collect-style
        # callers (CLI --save-results).  Keyed by project path str -> action source ->
        # {resultByFormat, returnCode}, matching the runBatch response `results` shape.
        merged_results: dict[str, dict[str, BatchSlotResult]] = {}
        # Optional mixed progress pipeline: when the batch request
        # also carries a progressToken, each non-matrix action context is
        # given its existing child progress identity and its progress events
        # are forwarded through the same slot aggregation pipeline used by
        # the progress-only wrapper. Matrix variants keep their documented
        # omission of variant progress tokens; their partials still drain.
        from finecode.wm_server.services import (
            partial_results_service as _prs_for_slots,
        )

        requested_progress_token: str | int | None = params.get("progressToken")
        batch_progress_tokens: dict[tuple[pathlib.Path, str], str] = {}
        batch_slot_streams: dict[
            str, stream_completion.CompletionStream[domain.ProgressRawValue]
        ] = {}
        batch_combined: (
            stream_completion.CompletionStream[domain.ProgressRawValue] | None
        ) = None
        batch_aggregator: typing.Any = None
        if requested_progress_token is not None:
            slot_total = sum(len(acts) for acts in actions_by_project.values())
            batch_combined = stream_completion.CompletionStream()
            batch_aggregator = _prs_for_slots.ProgressAggregator(
                slot_total, batch_combined
            )
            for _ppath, _acts in actions_by_project.items():
                _pdef = ws_context.ws_projects.get(_ppath)
                _pname = (
                    _pdef.name
                    if isinstance(_pdef, domain.CollectedProject)
                    else str(_ppath)
                )
                for _aname in _acts:
                    _skey = f"{_pname}/{_aname}"
                    if _skey not in batch_slot_streams:
                        batch_slot_streams[_skey] = stream_completion.CompletionStream()
                    batch_progress_tokens[(_ppath, _aname)] = f"progress-{uuid.uuid4()}"

        async def _stream_action(
            project_path: pathlib.Path,
            action_name: str,
            action_source: str,
            project_payload: dict,
        ) -> int:
            """Stream one action; emit partial notifications; return return code.

            The ER uses two mutually exclusive result delivery modes (see WM-ER protocol):
            - Streaming (status "streamed"): results arrive as $/progress notifications,
              ctx.partials yields each one, resultByFormat in the final RPC response is empty.
            - Direct (status "success"): no $/progress notifications; resultByFormat in
              ctx.responses holds the complete result.
            Both modes must produce an actions/partialResult notification to the CLI.
            """
            er_token = str(uuid.uuid4())

            project_def = ws_context.ws_projects.get(project_path)
            action_def = (
                next((a for a in project_def.actions if a.name == action_name), None)
                if isinstance(project_def, domain.CollectedProject)
                else None
            )
            if action_def is not None and matrix_runner.is_matrixed(action_def):
                selected_envs = selection_by_project.get(project_path)

                async def _on_partial(
                    interpreter_canonical: str, result_by_format: dict
                ) -> None:
                    async with writer_lock:
                        _notify_client(
                            writer,
                            "actions/partialResult",
                            {
                                "token": token,
                                "value": {
                                    "project": str(project_path),
                                    "interpreter": interpreter_canonical,
                                    "results": {
                                        action_source: {
                                            "resultByFormat": result_by_format,
                                            "returnCode": 0,
                                        }
                                    },
                                    "returnCode": 0,
                                },
                            },
                        )
                        await writer.drain()

                async def _on_prefix(
                    fence: stream_completion.PrefixFence,
                ) -> None:
                    # No envelope to send for a fence; the writer drain is
                    # the handoff that orders every preceding partial before
                    # the acknowledged prefix.
                    async with writer_lock:
                        await writer.drain()
                    if not fence.completion.done():
                        fence.completion.set_result(None)

                (
                    combined_rbf,
                    matrix_return_code,
                ) = await matrix_streaming.run_matrix_with_partial_results(
                    project=project_def,
                    action=action_def,
                    action_name=action_name,
                    params=project_payload,
                    result_formats=parsed.result_formats,
                    partial_result_token=er_token,
                    run_trigger=parsed.trigger,
                    dev_env=parsed.dev_env,
                    ws_context=ws_context,
                    merge_results=parsed.merge_results,
                    on_partial=_on_partial,
                    on_prefix=_on_prefix,
                    selected_envs=selected_envs,
                    origin=origin,
                )
                if parsed.merge_results:
                    merged_results.setdefault(str(project_path), {})[action_source] = {
                        "resultByFormat": typing.cast(
                            _internal_client_types.ResultByFormat, combined_rbf
                        ),
                        "returnCode": matrix_return_code,
                    }
                return matrix_return_code

            action_return_code = 0
            partial_count = 0
            entries: list[_internal_client_types.ResultByFormat] = []
            result_formats_for_run = list(parsed.result_formats)
            if (
                parsed.merge_results
                and runner_client.RunResultFormat.STRING in result_formats_for_run
                and runner_client.RunResultFormat.JSON not in result_formats_for_run
            ):
                result_formats_for_run.append(runner_client.RunResultFormat.JSON)
            child_progress_token = batch_progress_tokens.get(
                (project_path, action_name)
            )
            project_def_for_slot = ws_context.ws_projects.get(project_path)
            slot_name = (
                project_def_for_slot.name
                if isinstance(project_def_for_slot, domain.CollectedProject)
                else str(project_path)
            )
            slot_key = f"{slot_name}/{action_name}"
            slot_stream = batch_slot_streams.get(slot_key)
            async with proxy_utils.run_with_partial_results(
                action_name=action_name,
                params=project_payload,
                partial_result_token=er_token,
                project_dir_path=project_path,
                run_trigger=parsed.trigger,
                dev_env=parsed.dev_env,
                ws_context=ws_context,
                initialize_all_handlers=True,
                result_formats=result_formats_for_run,
                progress_token=child_progress_token,
                origin=origin,
            ) as ctx:

                async def _forward_mixed_partials() -> None:
                    nonlocal partial_count
                    async for value in ctx:
                        partial_count += 1
                        if parsed.merge_results and isinstance(value, dict):
                            entries.append(
                                typing.cast(
                                    _internal_client_types.ResultByFormat, value
                                )
                            )
                        async with writer_lock:
                            _notify_client(
                                writer,
                                "actions/partialResult",
                                {
                                    "token": token,
                                    "value": {
                                        "project": str(project_path),
                                        "results": {
                                            action_source: {
                                                "resultByFormat": strip_unrequested_formats(
                                                    typing.cast(
                                                        _internal_client_types.ResultByFormat,
                                                        value,
                                                    ),
                                                    parsed.result_formats,
                                                ),
                                                "returnCode": 0,
                                            }
                                        },
                                        "returnCode": 0,
                                    },
                                },
                            )
                            await writer.drain()

                async def _forward_mixed_progress() -> None:
                    if (
                        child_progress_token is None
                        or ctx.progress is None
                        or slot_stream is None
                    ):
                        return
                    try:
                        async for event in ctx.progress.events():
                            if isinstance(event, stream_completion.PrefixFence):
                                slot_stream.forward(event)
                            else:
                                slot_stream.append(event.value)
                    except BaseException as exc:
                        fault = (
                            exc
                            if isinstance(exc, Exception)
                            else stream_completion.PipelineFailure(exc)
                        )
                        slot_stream.abort(fault)
                        raise

                if slot_stream is not None and ctx.progress is not None:
                    async with asyncio.TaskGroup() as mixed_group:
                        _ = mixed_group.create_task(_forward_mixed_partials())
                        _ = mixed_group.create_task(_forward_mixed_progress())
                else:
                    await _forward_mixed_partials()
            for resp in ctx.responses:
                action_return_code |= resp.return_code
            # Direct result mode: no $/progress notifications were sent, so the complete
            # result sits in ctx.responses. Forward it as a single partialResult notification.
            if partial_count == 0 and ctx.responses:
                final_result_by_format: _internal_client_types.ResultByFormat = {}
                for resp in ctx.responses:
                    if resp.status != "streamed" and not resp.result_by_format:
                        raise ActionRunFailed(
                            f"ER returned empty result with status '{resp.status}'"
                            f" for action={action_name} project={project_path};"
                            " expected 'streamed' status when result_by_format is empty"
                        )
                    final_result_by_format.update(
                        typing.cast(
                            _internal_client_types.ResultByFormat,
                            resp.result_by_format,
                        )
                    )
                if final_result_by_format:
                    if parsed.merge_results:
                        entries.append(final_result_by_format)
                    async with writer_lock:
                        _notify_client(
                            writer,
                            "actions/partialResult",
                            {
                                "token": token,
                                "value": {
                                    "project": str(project_path),
                                    "results": {
                                        action_source: {
                                            "resultByFormat": strip_unrequested_formats(
                                                final_result_by_format,
                                                parsed.result_formats,
                                            ),
                                            "returnCode": action_return_code,
                                        }
                                    },
                                    "returnCode": action_return_code,
                                },
                            },
                        )
                        await writer.drain()

            # Merge every partial streamed for this (project, action) into one result
            # for the response, so a caller that replaces-by-key keeps complete data
            # rather than only the last partial.
            if parsed.merge_results:
                try:
                    merged_result = await merge_slot_results(
                        project_path=project_path,
                        action_name=action_name,
                        entries=entries,
                        requested_formats=parsed.result_formats,
                        ws_context=ws_context,
                    )
                except ActionRunFailed as exc:
                    if (
                        runner_client.RunResultFormat.STRING
                        not in parsed.result_formats
                    ):
                        raise
                    action_return_code |= 1
                    merged_results.setdefault(str(project_path), {})[action_source] = {
                        "resultByFormat": {},
                        "returnCode": action_return_code,
                    }
                    logger.bind(project=str(project_path), action=action_name).error(
                        f"Failed to merge action result: {exc.message}"
                    )
                    return action_return_code
                if merged_result is not None:
                    merged_results.setdefault(str(project_path), {})[action_source] = {
                        "resultByFormat": merged_result,
                        "returnCode": action_return_code,
                    }
            return action_return_code

        async def _stream_project(
            project_path: pathlib.Path,
            actions_to_run: list,
            project_payload: dict,
        ) -> int:
            """Stream all actions for one project; return project-level return code."""
            project_return_code = 0
            for action_name in actions_to_run:
                action_source = name_to_source.get(action_name, action_name)
                rc = await _stream_action(
                    project_path, action_name, action_source, project_payload
                )
                project_return_code |= rc
            return project_return_code

        # Launch one task per project for project-level concurrency. The
        # machine-wide process budget (ADR-0090), leased to each project's ER
        # when its run begins, is what bounds the subprocess fan-out these
        # tasks lead to.
        mixed_pump_tasks: list[asyncio.Task[None]] = []
        mixed_sink_task: asyncio.Task[None] | None = None
        if batch_combined is not None and batch_aggregator is not None:
            assert requested_progress_token is not None
            for _skey, _slot in batch_slot_streams.items():
                mixed_pump_tasks.append(
                    asyncio.create_task(
                        _pump_slot_to_combined(
                            _slot, _skey, batch_aggregator, batch_combined
                        )
                    )
                )
            mixed_sink_task = asyncio.create_task(
                _forward_combined_to_client(
                    batch_combined,
                    writer,
                    requested_progress_token,
                    writer_lock,
                )
            )
        project_tasks: dict[pathlib.Path, asyncio.Task] = {}
        for project_path, actions_to_run in actions_by_project.items():
            project_payload = {
                **parsed.action_params,
                **payload_overrides.get(str(project_path), {}),
            }
            task = asyncio.create_task(
                _stream_project(project_path, actions_to_run, project_payload)
            )
            project_tasks[project_path] = task

        overall_return_code = 0
        project_remaining: set[asyncio.Task] = set(project_tasks.values())
        forwarder_tasks: set[asyncio.Task] = set(mixed_pump_tasks)
        if mixed_sink_task is not None:
            forwarder_tasks.add(mixed_sink_task)

        try:
            # Project tasks complete with return codes; pump/sink tasks
            # complete only on fault (they wait for the stream ends in the
            # finally below). A broken forwarder therefore aborts the
            # executor through the cancellation below, while a received
            # runner outcome waits for its prefix inside the runner boundary
            # before surfacing here. Cancellation joins project tasks even
            # when it interrupts this wait loop (finally below).
            while project_remaining:
                done, _pending = await asyncio.wait(
                    project_remaining | forwarder_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        for t in project_remaining | forwarder_tasks:
                            if t is not task:
                                t.cancel()
                        await asyncio.gather(
                            *(project_remaining | forwarder_tasks),
                            return_exceptions=True,
                        )
                        raise exc
                    if task in project_remaining:
                        project_remaining.discard(task)
                        overall_return_code |= int(task.result())
                    else:
                        # A forwarder finishing before its stream end is a
                        # programming error; the remaining owners are cancelled.
                        for t in project_remaining | forwarder_tasks:
                            if t is not task:
                                t.cancel()
                        await asyncio.gather(
                            *(project_remaining | forwarder_tasks),
                            return_exceptions=True,
                        )
                        raise ActionRunFailed(
                            "progress forwarder finished before stream end"
                        )
        finally:
            if batch_combined is not None:
                for _slot in batch_slot_streams.values():
                    _slot.end()
                batch_combined.end()
            for _pt in forwarder_tasks:
                if not _pt.done():
                    _pt.cancel()
            await asyncio.gather(*forwarder_tasks, return_exceptions=True)

        logger.debug(
            f"runBatch+partialResults: done, projects_count={len(actions_by_project)}"
            f" returnCode={overall_return_code}"
        )
        ordered_results: dict[str, dict[str, BatchSlotResult]] = {}
        for project_path, actions_to_run in actions_by_project.items():
            project_str = str(project_path)
            project_results = merged_results.get(project_str)
            if project_results is None:
                continue
            ordered_actions: dict[str, BatchSlotResult] = {}
            for action_name in actions_to_run:
                action_source = name_to_source.get(action_name, action_name)
                if action_source in project_results:
                    ordered_actions[action_source] = project_results[action_source]
            if ordered_actions:
                ordered_results[project_str] = ordered_actions

        response: dict = {"returnCode": overall_return_code}
        if parsed.merge_results:
            response["results"] = ordered_results
        return response


async def _handle_run_batch_with_partial_results_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task wrapper for ``actions/runBatch`` with ``partialResultToken``."""
    try:
        result = await _handle_run_batch_with_partial_results(
            params, ws_context, writer
        )
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(writer, _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)))
        await writer.drain()
    except ActionNotFoundError as exc:
        logger.error(
            f"FineCode API: error handling actions/runBatch with partialResultToken: {exc.message}"
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, exc.message))
        await writer.drain()
    except ActionRunFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/runBatch with partialResultToken",
            exc.message,
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except StartingEnvironmentsFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/runBatch with partialResultToken",
            exc.message,
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except Exception as exc:
        logger.exception(
            "FineCode API: error handling actions/runBatch with partialResultToken"
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()


def _distinct_progress_runners(
    runners_by_env: dict[str, typing.Any],
    handlers: list[typing.Any],
) -> list[typing.Any]:
    """Deduplicate same-env handlers to one runner per env."""
    seen: set[int] = set()
    distinct: list[typing.Any] = []
    for handler in handlers:
        runner = runners_by_env.get(handler.env)
        if runner is not None and id(runner) not in seen:
            seen.add(id(runner))
            distinct.append(runner)
    return distinct


async def _pump_subscription_to_output(
    subscription: typing.Any,
    output: stream_completion.CompletionStream[typing.Any],
) -> None:
    """Forward subscription events; intermediate stage never acknowledges."""
    try:
        async for event in subscription.stream.events():
            if isinstance(event, stream_completion.PrefixFence):
                output.forward(event)
            else:
                output.append(event.value)
    except BaseException as exc:
        fault: Exception = (
            exc
            if isinstance(exc, Exception)
            else stream_completion.PipelineFailure(exc)
        )
        output.abort(fault)
        raise


async def _pump_slot_to_combined(
    slot: stream_completion.CompletionStream[typing.Any],
    slot_key: str,
    aggregator: typing.Any,
    combined: stream_completion.CompletionStream[typing.Any],
) -> None:
    """Forward slot events through aggregation; fences survive suppressed output."""
    try:
        async for event in slot.events():
            if isinstance(event, stream_completion.PrefixFence):
                combined.forward(event)
            else:
                aggregator.on_progress(slot_key, event.value)
    except BaseException as exc:
        fault = (
            exc
            if isinstance(exc, Exception)
            else stream_completion.PipelineFailure(exc)
        )
        combined.abort(fault)
        raise


async def _forward_combined_to_client(
    combined: stream_completion.CompletionStream[typing.Any],
    writer: asyncio.StreamWriter,
    token: str | int,
    writer_lock: asyncio.Lock | None = None,
) -> None:
    """Final-sink iteration: acknowledge each fence only after drain resumes."""
    async for value in combined:
        _notify_client(
            writer,
            "actions/progress",
            {"token": token, "value": value},
        )
        if writer_lock is not None:
            async with writer_lock:
                await writer.drain()
        else:
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
    with telemetry.attach_incoming_traceparent(params):
        from finecode.wm_server.services import (
            project_resolution_service,
            run_service,
        )

        try:
            parsed = await _parse_and_validate_run_action_params(params, ws_context)
        except project_resolution_service.ProjectResolutionFailed as exc:
            raise ValueError(exc.message) from exc
        progress_token = params["progressToken"]

        # Ensure runners are started before subscribing to progress notifications
        # so the runner objects exist in ws_projects_extension_runners.
        await run_service.start_required_environments(
            {parsed.project.dir_path: [parsed.action.name]},
            ws_context,
            initialize_all_handlers=True,
        )

        runners_by_env = ws_context.ws_projects_extension_runners.get(
            parsed.project.dir_path, {}
        )
        distinct_runners = _distinct_progress_runners(
            runners_by_env, list(parsed.action.handlers)
        )
        combined: stream_completion.CompletionStream[domain.ProgressRawValue] = (
            stream_completion.CompletionStream()
        )
        # Synchronously owned subscriptions: registered before dispatch so
        # immediate emissions on either channel are captured. The logical
        # stream stays open across per-invocation drains for later segments.
        with contextlib.ExitStack() as owned:
            subscriptions = [
                owned.enter_context(
                    runner.stream_registry.subscribe_progress(progress_token)
                )
                for runner in distinct_runners
            ]
            result_box: list[typing.Any] = []

            async def _execute() -> None:
                executor = run_service.ProjectExecutor(ws_context)
                try:
                    result_box.append(
                        await executor.run_action(
                            action_source=parsed.action.canonical_source,
                            params=parsed.action_params,
                            project_path=parsed.project.dir_path,
                            run_trigger=parsed.trigger,
                            dev_env=parsed.dev_env,
                            result_formats=parsed.result_formats,
                            initialize_all_handlers=True,
                            progress_token=progress_token,
                            # Progress is forwarded to this connection for the
                            # whole run, so it is just as much the run's origin
                            # as on the partial-results paths.
                            origin=elicitation_bridge.RunDispatchOrigin(
                                connection=writer,
                                request_task=asyncio.current_task(),
                            ),
                        )
                    )
                finally:
                    # A received runner outcome already drained its prefix
                    # through the pumps/sink below; ending lets the pumps and
                    # sink observe remaining values before the outcome
                    # surfaces. Subscription streams end here so their pumps
                    # exit; the logical combined stream stays open across
                    # per-invocation drains and ends only with the run. A pump
                    # fault already aborted the stream; end() is a no-op.
                    for subscription in subscriptions:
                        subscription.stream.end()
                    combined.end()

            async with asyncio.TaskGroup() as task_group:
                for subscription in subscriptions:
                    _ = task_group.create_task(
                        _pump_subscription_to_output(subscription, combined)
                    )
                _ = task_group.create_task(
                    _forward_combined_to_client(combined, writer, progress_token)
                )
                _ = task_group.create_task(_execute())
            result = result_box[0]
            return {
                "resultByFormat": result.result_by_format,
                "returnCode": result.return_code,
            }


async def _handle_run_action_with_progress_task(
    params: dict | None,
    ws_context: context.WorkspaceContext,
    writer: asyncio.StreamWriter,
    req_id: int | str,
) -> None:
    """Task wrapper for ``actions/run`` with progress — mirrors
    ``_handle_run_with_partial_results_task``."""
    try:
        result = await _handle_run_action_with_progress(params, ws_context, writer)
        _write_message(writer, _jsonrpc_response(req_id, result))
        await writer.drain()
    except _NotImplementedError as exc:
        _write_message(
            writer,
            _jsonrpc_error(req_id, NOT_IMPLEMENTED_CODE, str(exc)),
        )
        await writer.drain()
    except ActionNotFoundError as exc:
        logger.error("FineCode API: error handling actions/run with progress: {}", exc)
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except ActionRunFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/run with progress", str(exc)
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except StartingEnvironmentsFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/run with progress", exc.message
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except BaseExceptionGroup as exc:
        # The supervised TaskGroup wraps the executor outcome: a single
        # runner outcome keeps its existing wrapper code/text (runner-only
        # stays primary on dual faults).
        runner_outcome = next(
            (sub for sub in exc.exceptions if isinstance(sub, ActionRunFailed)),
            None,
        )
        if runner_outcome is None:
            runner_outcome = next(
                (
                    sub
                    for sub in exc.exceptions
                    if isinstance(sub, StartingEnvironmentsFailed)
                ),
                None,
            )
        if runner_outcome is not None:
            msg = client_message(
                "FineCode API: error handling actions/run with progress",
                runner_outcome.message,
            )
            _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        else:
            logger.exception("FineCode API: error handling actions/run with progress")
            _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except Exception as exc:
        logger.exception("FineCode API: error handling actions/run with progress")
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
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
    from finecode.wm_server.services import (
        partial_results_service,
        project_resolution_service,
        run_service,
    )

    params = params or {}
    with telemetry.attach_incoming_traceparent(params):
        parsed = _parse_run_batch_params(params)
        progress_token: str = params["progressToken"]

        if not parsed.action_sources:
            raise ValueError("actionSources list is required and must be non-empty")

        logger.debug(
            f"runBatch+progress: actionSources={parsed.action_sources} projects={parsed.project_names}"
        )

        try:
            actions_by_project, name_to_source = await _resolve_actions_by_project(
                parsed.project_names,
                parsed.action_sources,
                ws_context,
                run_options=params.get("options") or {},
            )
        except project_resolution_service.ProjectResolutionFailed as exc:
            raise ActionRunFailed(exc.message) from exc

        await run_service.start_required_environments(actions_by_project, ws_context)

        # One aggregation slot per (project × action) pair.
        # Each slot gets a unique internal progress token sent to its ER.
        slot_count = sum(len(acts) for acts in actions_by_project.values())
        combined_stream: stream_completion.CompletionStream[domain.ProgressRawValue] = (
            stream_completion.CompletionStream()
        )
        aggregator = partial_results_service.ProgressAggregator(
            slot_count, combined_stream
        )

        # progress_token_by_project[project_path][action_name] = internal_token
        progress_token_by_project: dict[pathlib.Path, dict[str, str]] = {}
        slot_streams: dict[
            str, stream_completion.CompletionStream[domain.ProgressRawValue]
        ] = {}
        # (subscription, slot) pairs for synchronously owned registrations.
        owned_pairs: list[
            tuple[
                typing.Any, stream_completion.CompletionStream[domain.ProgressRawValue]
            ]
        ] = []

        for project_path, actions_to_run in actions_by_project.items():
            project_def = ws_context.ws_projects[project_path]
            if not isinstance(project_def, domain.CollectedProject):
                continue
            progress_token_by_project[project_path] = {}
            runners_by_env = ws_context.ws_projects_extension_runners.get(
                project_path, {}
            )

            for action_name in actions_to_run:
                internal_token = f"progress-{uuid.uuid4()}"
                progress_token_by_project[project_path][action_name] = internal_token
                slot_key = f"{project_def.name}/{action_name}"
                slot_stream: stream_completion.CompletionStream[
                    domain.ProgressRawValue
                ] = stream_completion.CompletionStream()
                slot_streams[slot_key] = slot_stream

        with contextlib.ExitStack() as owned:
            for project_path, actions_to_run in actions_by_project.items():
                project_def = ws_context.ws_projects[project_path]
                if not isinstance(project_def, domain.CollectedProject):
                    continue
                runners_by_env = ws_context.ws_projects_extension_runners.get(
                    project_path, {}
                )
                for action_name in actions_to_run:
                    internal_token = progress_token_by_project[project_path][
                        action_name
                    ]
                    slot_key = f"{project_def.name}/{action_name}"
                    slot_stream = slot_streams[slot_key]
                    action_def = next(
                        (a for a in project_def.actions if a.name == action_name), None
                    )
                    if action_def is None:
                        continue
                    for runner in _distinct_progress_runners(
                        runners_by_env, list(action_def.handlers)
                    ):
                        subscription = owned.enter_context(
                            runner.stream_registry.subscribe_progress(internal_token)
                        )
                        owned_pairs.append((subscription, slot_stream))

            result_box: list[typing.Any] = []

            async def _execute_batch() -> None:
                workspace_executor = run_service.WorkspaceExecutor(ws_context)
                try:
                    result_box.append(
                        await workspace_executor.run_actions_in_projects(
                            actions_by_project=actions_by_project,
                            params=parsed.action_params,
                            run_trigger=parsed.trigger,
                            dev_env=parsed.dev_env,
                            concurrently=parsed.concurrently,
                            result_formats=parsed.result_formats,
                            payload_overrides_by_project=parsed.params_by_project
                            or None,
                            progress_token_by_project=progress_token_by_project,
                            # Aggregated progress goes to this connection until
                            # the batch ends, so it is the origin of every run.
                            origin=elicitation_bridge.RunDispatchOrigin(
                                connection=writer,
                                request_task=asyncio.current_task(),
                            ),
                        )
                    )
                finally:
                    # Slot/output streams end only after all producer
                    # discovery/invocations complete; received prefixes already
                    # drained through the pumps/sink before this return.
                    # Subscription streams end so their pumps exit; slot and
                    # combined ends let the remaining stages drain.
                    for subscription, _slot_stream in owned_pairs:
                        subscription.stream.end()
                    for slot_stream in slot_streams.values():
                        slot_stream.end()
                    combined_stream.end()

            async with asyncio.TaskGroup() as batch_group:
                for subscription, slot_stream in owned_pairs:
                    _ = batch_group.create_task(
                        _pump_subscription_to_output(subscription, slot_stream)
                    )
                for slot_key, slot_stream in slot_streams.items():
                    _ = batch_group.create_task(
                        _pump_slot_to_combined(
                            slot_stream, slot_key, aggregator, combined_stream
                        )
                    )
                _ = batch_group.create_task(
                    _forward_combined_to_client(combined_stream, writer, progress_token)
                )
                _ = batch_group.create_task(_execute_batch())
            result_by_project = result_box[0]

        results, overall_return_code = _build_batch_result(
            result_by_project, name_to_source
        )
        logger.debug(
            f"runBatch+progress: done, projects_count={len(results)} returnCode={overall_return_code}"
        )
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
    except ActionNotFoundError as exc:
        logger.error(
            f"FineCode API: error handling actions/runBatch with progress: {exc.message}"
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, exc.message))
        await writer.drain()
    except ActionRunFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/runBatch with progress", exc.message
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except StartingEnvironmentsFailed as exc:
        msg = client_message(
            "FineCode API: error handling actions/runBatch with progress", exc.message
        )
        _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        await writer.drain()
    except BaseExceptionGroup as exc:
        # The supervised TaskGroup wraps the executor outcome: a single
        # runner outcome keeps its existing wrapper code/text (runner-only
        # stays primary on dual faults).
        runner_outcome = next(
            (sub for sub in exc.exceptions if isinstance(sub, ActionRunFailed)),
            None,
        )
        if runner_outcome is None:
            runner_outcome = next(
                (
                    sub
                    for sub in exc.exceptions
                    if isinstance(sub, StartingEnvironmentsFailed)
                ),
                None,
            )
        if runner_outcome is not None:
            msg = client_message(
                "FineCode API: error handling actions/runBatch with progress",
                runner_outcome.message,
            )
            _write_message(writer, _jsonrpc_error(req_id, -32603, msg))
        else:
            logger.exception(
                "FineCode API: error handling actions/runBatch with progress"
            )
            _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()
    except Exception as exc:
        logger.exception("FineCode API: error handling actions/runBatch with progress")
        _write_message(writer, _jsonrpc_error(req_id, -32603, str(exc)))
        await writer.drain()


async def _resolve_source_to_name(
    action_source: str,
    project_path_str: str,
    ws_context: context.WorkspaceContext,
    *,
    run_options: dict | None = None,
) -> str:
    """Resolve an action source to its internal name for the partial-results pipeline.

    When *project_path_str* is the empty string the action is resolved against
    the first available :class:`~domain.CollectedProject` that declares it.
    This mirrors the "all projects" semantic supported by
    :func:`~partial_results_service.run_action_with_partial_results`.

    Raises ``ValueError`` if the project or action cannot be found.
    """
    from finecode.wm_server._api_handlers._helpers import (
        find_action_by_source,
    )
    from finecode.wm_server.services import project_resolution_service

    if project_path_str:
        path = pathlib.Path(project_path_str)
        project = ws_context.ws_projects.get(path)
        if project is None:
            raise ValueError(
                f"Project '{project_path_str}' not found or not initialized"
            )

        project = (
            await project_resolution_service.ensure_projects_resolved(
                [path], ws_context
            )
        ).require([path])[0]
        action = await find_action_by_source(
            project.actions, action_source, project, ws_context
        )
        if action is None:
            raise ActionNotFoundError(
                f"Action with source '{action_source}' not found in project '{project_path_str}'"
            )
        return action.name
    else:
        # project="" means "all projects" — resolve against who hosts the
        # action
        hosting = await project_resolution_service.resolve_hosting_projects(
            [action_source], "source", ws_context, run_options=run_options
        )
        for project in hosting.outcome.resolved.values():
            action = await find_action_by_source(
                project.actions, action_source, project, ws_context
            )
            if action is not None:
                return action.name
        if hosting.outcome.failed:
            raise project_resolution_service.ProjectResolutionFailed(
                "; ".join(
                    f"{p}: {reason}" for p, reason in hosting.outcome.failed.items()
                )
            )
        raise ActionNotFoundError(
            f"Action with source '{action_source}' not found in any project"
        )
