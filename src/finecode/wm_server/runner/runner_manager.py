"""
API to manage ERs: start, stop, restart.
"""

import asyncio
import collections.abc
import concurrent.futures
import contextlib
import dataclasses
import json
import os
import shutil
import time
import typing
from pathlib import Path

from loguru import logger

import finecode_jsonrpc as jsonrpc_client
from finecode import telemetry
from finecode.wm_server import context, domain, domain_helpers, errors, host_pressure
from finecode.wm_server.config import collect_actions, config_models, read_configs
from finecode.wm_server.runner import (
    _internal_client_api,
    _internal_client_types,
    apply_workspace_edit_bridge,
    elicitation_bridge,
    finecode_cmd,
    knowledge_bridge,
    preset_resolution,
    run_dispatch_bridge,
    runner_client,
    wm_bridge,
)
from finecode_jsonrpc import _io_thread

project_changed_callback: (
    typing.Callable[[domain.Project], collections.abc.Coroutine[None, None, None]]
    | None
) = None
# get_document: typing.Callable[[], collections.abc.Coroutine] | None = None
start_debug_session: typing.Callable[[int], collections.abc.Coroutine] | None = None

# reexport
RunnerFailedToStart = jsonrpc_client.ServerFailedToStart
ServerConfigurationError = config_models.ConfigurationError

# The ER reports this when its installed distributions no longer match the
# configuration it was handed — a dependency added to pyproject.toml without
# reinstalling the environment, typically.
_ENV_REINSTALL_NEEDED_ERROR_CODE = -32001

SLOW_START_WARN_SEC: typing.Final = 10.0


class EnvironmentOutOfDateError(RunnerFailedToStart):
    """The runner's environment no longer satisfies its configuration.

    A subclass so that every ``except RunnerFailedToStart`` — including the
    auto-repair path — keeps catching it, while a caller that reports the
    failure can name the environment command that fixes it rather than
    surfacing an import error.

    Carries ``env_name`` because the command that fixes it names one: a caller
    that recovers a whole project has no single environment of its own to put
    there, and the one that went stale is known only here.
    """

    def __init__(self, message: str, env_name: str | None = None) -> None:
        super().__init__(message)
        self.env_name = env_name


async def notify_project_changed(project: domain.Project) -> None:
    if project_changed_callback is not None:
        await project_changed_callback(project)


def handle_er_log_records(
    runner: runner_client.ExtensionRunnerInfo, params: dict
) -> None:
    """Tag source and feed ER records into the Phase-1 delivery pipeline.

    Runs on the loop thread (feature callback), so delivery is safe. Redaction
    happens in the bridge implementation, at the WM boundary.
    """
    bridge = wm_bridge.handlers()
    source = f"runner:{runner.env_name}@{runner.working_dir_path.name}"
    for r in (params or {}).get("records", []):
        bridge.deliver_er_log_record(
            source=source,
            timestamp=r.get("timestamp", 0.0),
            level=r.get("level", "INFO"),
            group=r.get("group", ""),
            message=r.get("message", ""),
        )


async def _apply_workspace_edit(
    params: _internal_client_types.ApplyWorkspaceEditParams,
):
    """Forward an ER's apply-edit request to the editor, preserving array order.

    ``documentChanges`` is ordered and re-ordering it changes what it means, so
    the mixed create/rename/delete/text-edit array is passed through exactly as
    it arrived. An operation the editor cannot perform fails the request with
    an explicit error rather than being dropped: this direction has a caller
    waiting on a result.
    """
    bridge = apply_workspace_edit_bridge.handlers()
    if bridge is None:
        raise errors.InternalError(
            "No editor connection is installed, so workspace/applyEdit cannot be answered"
        )

    supported = bridge.supported_resource_operations()
    for change in params.edit.document_changes or []:
        if (
            isinstance(
                change,
                (
                    _internal_client_types.CreateFile,
                    _internal_client_types.RenameFile,
                    _internal_client_types.DeleteFile,
                ),
            )
            and change.kind not in supported
        ):
            raise errors.InternalError(
                f"the editor cannot perform the {change.kind!r} operation"
            )

    return await bridge.apply_workspace_edit(params)


def resolve_lease_terms(
    ws_context: context.WorkspaceContext,
    *,
    requested: int,
    nested: bool,
    run_id: str | None,
) -> tuple[int, bool]:
    """(requested, nested) for a lease, after the run's declared RunBudget (ADR-0094).

    A dispatch may declare how the process budget should treat its leases —
    ``waits`` overrides the ER's nesting flag and ``max_slots`` caps the
    requested width. An unknown run, or one dispatched without a run id, keeps
    the ER's own values. The run is found by scanning the in-flight entries for
    its id rather than by the runner's project path, so a project-key mismatch
    cannot silently miss it.
    """
    if run_id is not None:
        for runs in ws_context.in_flight_runs.values():
            run = runs.get(run_id)
            if run is None:
                continue
            budget = run.budget
            if budget.waits is not None:
                nested = not budget.waits
            if budget.max_slots is not None:
                requested = min(requested, budget.max_slots)
            break
    return requested, nested


async def _start_extension_runner_process(
    runner: runner_client.ExtensionRunnerInfo,
    ws_context: context.WorkspaceContext,
    debug: bool = False,
) -> None:
    try:
        if runner.cmd_override:
            python_cmd = runner.cmd_override
        else:
            python_cmd = finecode_cmd.get_python_cmd(
                runner.working_dir_path, runner.env_name
            )
    except ValueError as exception:
        if isinstance(exception, finecode_cmd.VenvRelocatedError):
            # The venv exists but was created at a different (now stale) path — its
            # console scripts have broken shebangs. Reinstalling in place wouldn't
            # fix them (pip/uv skip already-satisfied packages), so wipe it and let
            # the NO_VENV auto-repair path below do a genuine from-scratch create.
            logger.warning(str(exception))
            await remove_runner_env(runner.working_dir_path, runner.env_name)

        try:
            runner.status = runner_client.RunnerStatus.NO_VENV
            await notify_project_changed(
                ws_context.ws_projects[runner.working_dir_path]
            )
        except KeyError:
            ...
        logger.error(
            f"Project {runner.working_dir_path} uses finecode, but env (venv) doesn't exist yet. Run `prepare_env` command to create it"
        )

        raise RunnerFailedToStart(
            f"Runner '{runner.readable_id}' failed to start"
        ) from exception

    if ws_context.runner_io_thread is None:
        logger.trace("Starting IO Thread")
        ws_context.runner_io_thread = _io_thread.AsyncIOThread()
        ws_context.runner_io_thread.start()

    _project = ws_context.ws_projects[runner.working_dir_path]
    _default_env_config = domain.EnvConfig(
        runner_config=domain.RunnerConfig(debug=False)
    )
    # `dev_workspace` runner is started before the project config is fully collected, so
    # `env_configs` are unavailable here for it; `defaultLevel` is applied later via
    # `update_runner_config`
    env_config = (
        _project.env_configs.get(runner.env_name, _default_env_config)
        if isinstance(_project, domain.CollectedProject)
        else _default_env_config
    )
    runner_config = env_config.runner_config

    log_level = runner_config.logging.default_level
    process_args: list[str] = [
        f"--log-level={log_level}",
        f"--project-path={runner.working_dir_path.as_posix()}",
        f"--env-name={runner.env_name}",
    ]
    if ws_context.wal_writer is not None:
        process_args.append("--wal")

    start_with_debug = debug or runner_config.debug
    if start_with_debug:
        process_args.append("--debug")
        debug_port_future = concurrent.futures.Future()
    else:
        debug_port_future = None

    process_args_str: str = " ".join(process_args)
    client = jsonrpc_client.JsonRpcClient(
        message_types=_internal_client_types.METHOD_TO_TYPES,
        readable_id=runner.readable_id,
        tracing=telemetry.JsonRpcTracingHooks(),
    )
    # Attach before start() so a shutdown sweep racing with an in-flight start
    # attempt (subprocess already spawned, port handshake not yet resolved)
    # still has a handle to force-kill it — client.pid is only set once the
    # process actually exists, so force_kill() is a safe no-op before that.
    runner.client = client

    # Held from spawn until the RPC channel is confirmed connected — bounds how
    # many ERs are simultaneously mid-startup (CPU/memory-bursty: process spawn,
    # interpreter init, imports), regardless of which caller triggered this
    # start. Does NOT bound whatever the triggering action does afterward in
    # this ER's own process — that's a separate, much more variable resource
    # cost this cap deliberately leaves unconstrained. See ADR-0063.
    async with ws_context.er_startup_semaphore:
        try:
            await client.start(
                server_cmd=f"{python_cmd} -m finecode_extension_runner.cli start {process_args_str}",
                working_dir_path=runner.working_dir_path,
                io_thread=ws_context.runner_io_thread,
                debug_port_future=debug_port_future,
                connect=not start_with_debug,
            )
        except RunnerFailedToStart as exception:
            pressure = host_pressure.read_host_pressure()
            logger.bind(**pressure.fields()).error(
                f"Runner {runner.readable_id} failed to start: {exception.message};"
                f" host: {pressure.describe()}"
            )
            # client.start() may have already spawned the OS process (e.g. it timed
            # out waiting for the port handshake) — kill it now rather than leaving
            # it running unmanaged, since no status/attempt will ever revisit it.
            client.force_kill()
            await ws_context.process_budget.reclaim_for_runner(runner.readable_id)
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            raise

        timeline = client.startup_timeline
        if timeline.connected_at is not None and timeline.spawned_at is not None:
            spawn_to_output_ms = (
                None
                if timeline.first_output_at is None
                else round((timeline.first_output_at - timeline.spawned_at) * 1000)
            )
            spawn_to_port_ms = (
                None
                if timeline.port_line_at is None
                else round((timeline.port_line_at - timeline.spawned_at) * 1000)
            )
            spawn_to_connected_ms = round(
                (timeline.connected_at - timeline.spawned_at) * 1000
            )
            pressure = host_pressure.read_host_pressure()
            start_log = logger.bind(
                spawn_to_output_ms=spawn_to_output_ms,
                spawn_to_port_ms=spawn_to_port_ms,
                spawn_to_connected_ms=spawn_to_connected_ms,
                **pressure.fields(),
            )
            start_message = (
                f"Runner {runner.readable_id} start timeline:"
                f" {timeline.describe(time.monotonic())};"
                f" host: {pressure.describe()}"
            )
            if spawn_to_connected_ms >= SLOW_START_WARN_SEC * 1000:
                start_log.warning(start_message)
            else:
                start_log.debug(start_message)

        if start_with_debug:
            assert debug_port_future is not None

            # avoid blocking main thread?
            debug_async_future = asyncio.wrap_future(future=debug_port_future)
            try:
                await asyncio.wait_for(debug_async_future, timeout=30)
            except TimeoutError as exception:
                client.force_kill()
                await ws_context.process_budget.reclaim_for_runner(runner.readable_id)
                runner.status = runner_client.RunnerStatus.FAILED
                runner.initialized_event.set()
                raise RunnerFailedToStart(
                    f"Failed to get debugger port in 30 seconds: {runner.readable_id}"
                ) from exception

            debug_port = debug_async_future.result()
            logger.info(f"debug port: {debug_port}")

            if start_debug_session is not None:
                debug_params = {
                    "name": "Python: WM",
                    "type": "debugpy",
                    "request": "attach",
                    "connect": {"host": "localhost", "port": debug_port},
                    "justMyCode": False,
                    # "logToFile": True,
                }
                await start_debug_session(debug_params)

            try:
                await client.connect_to_server(
                    io_thread=ws_context.runner_io_thread, timeout=None
                )
            except Exception as exception:  # TODO: analyze which can occur
                logger.error(
                    f"Runner {runner.readable_id} failed to connect to server: {exception}"
                )
                client.force_kill()
                await ws_context.process_budget.reclaim_for_runner(runner.readable_id)
                runner.status = runner_client.RunnerStatus.FAILED
                runner.initialized_event.set()
                raise RunnerFailedToStart(str(exception)) from exception

    async def on_exit():
        logger.debug(f"Extension Runner {runner.readable_id} exited")
        if runner.status in (
            runner_client.RunnerStatus.RUNNING,
            runner_client.RunnerStatus.REPAIRING,
        ):
            telemetry.er_active_dec(runner.env_name)
        runner.status = runner_client.RunnerStatus.EXITED
        await notify_project_changed(
            ws_context.ws_projects[runner.working_dir_path]
        )  # TODO: fix
        # TODO: restart if WM is not stopping

    runner.client.server_exit_callback = on_exit

    runner.client.feature(
        _internal_client_types.WORKSPACE_APPLY_EDIT, _apply_workspace_edit
    )

    async def on_progress(params: _internal_client_types.ProgressParams) -> None:
        logger.debug(
            f"Got progress from runner {runner.readable_id} for token: {params.token}"
        )
        try:
            result_value = json.loads(params.value)
        except json.JSONDecodeError as exception:
            logger.error(f"Failed to decode partial result value json: {exception}")
            return

        # Distinguish progress notifications (begin/report/end) from partial results
        if isinstance(result_value, dict) and result_value.get("type") in (
            "begin",
            "report",
            "end",
        ):
            progress_notification = domain.ProgressNotification(
                token=params.token, value=result_value
            )
            runner.progress_notifications.publish(progress_notification)
        else:
            partial_result = domain.PartialResult(
                token=params.token, value=result_value
            )
            runner.partial_results.publish(partial_result)

    runner.client.feature(_internal_client_types.PROGRESS, on_progress)

    async def on_er_user_message(params) -> None:
        # params arrives as a structured ErUserMessageParams from the real client
        # (see METHOD_TO_TYPES); normalize so a raw dict from tests works too.
        if params is None:
            params_dict: dict = {}
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = dataclasses.asdict(params)

        wm_bridge.handlers().notify_all_clients(
            "server/userMessage",
            {
                "message": params_dict.get("message", ""),
                "type": params_dict.get("type", "WARNING"),
            },
        )

    runner.client.feature(_internal_client_types.ER_USER_MESSAGE, on_er_user_message)

    async def on_er_log_records(params) -> None:
        if params is None:
            params_dict: dict = {}
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = dataclasses.asdict(params)
        handle_er_log_records(runner, params_dict)

    runner.client.feature(_internal_client_types.ER_LOG_RECORDS, on_er_log_records)

    async def get_project_raw_config(
        params: _internal_client_types.GetProjectRawConfigParams,
    ):
        logger.debug(f"Get project raw config: {params}")
        project_def_path_str = params.project_def_path
        project_def_path = Path(project_def_path_str)
        try:
            project_raw_config = ws_context.ws_projects_raw_configs[
                project_def_path.parent
            ]
        except KeyError as exception:
            raise errors.InternalError(
                f"Config of project '{project_def_path_str}' not found"
            ) from exception
        return _internal_client_types.GetProjectRawConfigResult(
            config=project_raw_config
        )

    runner.client.feature(
        _internal_client_types.PROJECT_RAW_CONFIG_GET,
        get_project_raw_config,
    )

    async def get_workspace_packages(_params):
        return {"packages": ws_context.workspace_packages_wire()}

    runner.client.feature(
        _internal_client_types.WORKSPACE_PACKAGES_GET,
        get_workspace_packages,
    )

    async def get_workspace_extra_selection(_params):
        return {"selection": read_configs.read_workspace_extra_selection(ws_context)}

    runner.client.feature(
        _internal_client_types.WORKSPACE_EXTRA_SELECTION_GET,
        get_workspace_extra_selection,
    )

    _PROJECT_STATUS_MAP = {
        domain.ProjectStatus.CONFIG_VALID: "valid",
        domain.ProjectStatus.NO_FINECODE: "no_config",
        domain.ProjectStatus.CONFIG_INVALID: "invalid",
    }

    async def get_workspace_project_paths(_params):
        return {
            "projects": [
                {
                    "path": str(p.dir_path),
                    "configStatus": _PROJECT_STATUS_MAP.get(p.status, "invalid"),
                }
                for p in ws_context.ws_projects.values()
            ]
        }

    runner.client.feature(
        _internal_client_types.WORKSPACE_PROJECT_PATHS_GET,
        get_workspace_project_paths,
    )

    def _knowledge_handlers() -> knowledge_bridge.KnowledgeHandlers:
        """The installed knowledge service, or a method error naming why there is none.

        The runner cannot import the service (it sits a layer above); it is handed
        one. A WM built without it answers these two methods with an error, which
        is what an ER asking a WM that cannot serve knowledge should hear -- rather
        than a silent empty result that reads like "no facts".
        """
        installed = knowledge_bridge.handlers()
        if installed is None:
            raise errors.InternalError(
                "This WM has no knowledge service installed, so it cannot answer "
                "knowledge requests. Read facts through the in-process store instead."
            )
        return installed

    async def register_knowledge_schema(
        params: _internal_client_types.RegisterKnowledgeSchemaParams,
    ) -> _internal_client_types.RegisterKnowledgeSchemaResult:
        accepted = await _knowledge_handlers().register_schema(params.snapshot)
        return _internal_client_types.RegisterKnowledgeSchemaResult(accepted=accepted)

    runner.client.feature(
        _internal_client_types.KNOWLEDGE_REGISTER_SCHEMA,
        register_knowledge_schema,
    )

    async def run_knowledge_query(
        params: _internal_client_types.KnowledgeQueryParams,
    ) -> _internal_client_types.KnowledgeQueryResult:
        answered = await _knowledge_handlers().run_query(
            ws_context, params.query, mode=params.mode, limit=params.limit
        )
        return _internal_client_types.KnowledgeQueryResult(
            rows=answered["rows"], freshness=answered["freshness"]
        )

    runner.client.feature(
        _internal_client_types.KNOWLEDGE_QUERY,
        run_knowledge_query,
    )

    async def fetch_knowledge_records(
        params: _internal_client_types.KnowledgeRecordsParams,
    ) -> _internal_client_types.KnowledgeRecordsResult:
        found = await _knowledge_handlers().fetch_records(ws_context, params.refs)
        return _internal_client_types.KnowledgeRecordsResult(
            v=found["v"], records=found["records"]
        )

    runner.client.feature(
        _internal_client_types.KNOWLEDGE_RECORDS,
        fetch_knowledge_records,
    )

    def _run_dispatch_handlers() -> run_dispatch_bridge.RunDispatchHandlers:
        """The installed run-dispatch service, or a method error naming why there
        is none. Mirrors ``_knowledge_handlers`` above: the runner cannot import
        the service (it sits a layer above); it is handed one.
        """
        installed = run_dispatch_bridge.handlers()
        if installed is None:
            raise errors.InternalError(
                "This WM has no run-dispatch service installed, so it cannot "
                "execute ER-initiated actions."
            )
        return installed

    async def handle_run_action_in_project(
        params: _internal_client_types.RunActionInProjectParams,
    ) -> _internal_client_types.RunActionInProjectResult:
        return await _run_dispatch_handlers().run_action_in_project(
            runner, params, ws_context
        )

    runner.client.feature(
        _internal_client_types.RUN_ACTION_IN_PROJECT,
        handle_run_action_in_project,
    )

    async def handle_run_action_in_workspace(
        params: _internal_client_types.RunActionInWorkspaceParams,
    ) -> _internal_client_types.RunActionInWorkspaceResult:
        return await _run_dispatch_handlers().run_action_in_workspace(
            runner, params, ws_context
        )

    runner.client.feature(
        _internal_client_types.RUN_ACTION_IN_WORKSPACE,
        handle_run_action_in_workspace,
    )

    async def handle_lease_process_budget(
        params: _internal_client_types.LeaseProcessBudgetParams,
    ) -> _internal_client_types.LeaseProcessBudgetResult:
        """Lease process-budget slots for one action run in this ER (ADR-0090)."""
        requested, nested = resolve_lease_terms(
            ws_context,
            requested=params.requested,
            nested=params.nested,
            run_id=params.run_id,
        )
        lease = await ws_context.process_budget.lease(
            runner_id=runner.readable_id,
            requested=requested,
            nested=nested,
        )
        target = ws_context.process_budget.target_for_runner(runner.readable_id)
        await runner_client.update_process_budget(runner=runner, target=target)
        return _internal_client_types.LeaseProcessBudgetResult(
            lease_id=lease.lease_id, granted=lease.granted
        )

    runner.client.feature(
        _internal_client_types.LEASE_PROCESS_BUDGET,
        handle_lease_process_budget,
    )

    async def handle_release_process_budget(
        params: _internal_client_types.ReleaseProcessBudgetParams,
    ) -> _internal_client_types.ReleaseProcessBudgetResult:
        """Release one action run's process-budget lease (ADR-0090)."""
        await ws_context.process_budget.release(params.lease_id)
        target = ws_context.process_budget.target_for_runner(runner.readable_id)
        await runner_client.update_process_budget(runner=runner, target=target)
        return _internal_client_types.ReleaseProcessBudgetResult()

    runner.client.feature(
        _internal_client_types.RELEASE_PROCESS_BUDGET,
        handle_release_process_budget,
    )

    async def handle_get_actions_for_parent(
        params: _internal_client_types.GetActionsForParentParams,
    ) -> _internal_client_types.GetActionsForParentResult:
        """Serve ``finecode/getActionsForParent`` (ADR-0045). See
        ``run_service.er_dispatch._BridgeHandlers.get_actions_for_parent`` for
        the resolution logic.
        """
        return await _run_dispatch_handlers().get_actions_for_parent(
            runner, params, ws_context
        )

    runner.client.feature(
        _internal_client_types.GET_ACTIONS_FOR_PARENT,
        handle_get_actions_for_parent,
    )

    async def handle_list_workspace_actions(_params: dict | None = None) -> dict:
        """Serve ``finecode/listWorkspaceActions`` (ER → WM).

        Returns the aggregated action/handler registry across every project and
        env in the workspace. An ER only ever sees the actions its own env
        executes, so this cross-env picture can only come from the WM. Values
        come straight from the resolved ``domain.Action`` objects, including the
        ``file_loc`` each owning ER resolves via ``resolveActionMeta``; fields
        not yet resolved (before all ERs have started) are serialized as
        ``null``. Keys are camelCase for the JSON-RPC boundary.
        """
        actions: list[dict] = []
        for project in ws_context.ws_projects.values():
            if not isinstance(project, domain.CollectedProject):
                continue
            for action in project.actions:
                actions.append(
                    {
                        "name": action.name,
                        "source": action.source,
                        "canonicalSource": action.canonical_source,
                        "scope": (
                            action.scope.value if action.scope is not None else None
                        ),
                        "project": str(project.dir_path),
                        "language": action.language,
                        "parentActionSource": action.parent_action_source,
                        "fileLoc": action.file_loc,
                        "handlers": [
                            {
                                "name": h.name,
                                "source": h.source,
                                "env": h.env,
                                "fileLoc": h.file_loc,
                            }
                            for h in action.handlers
                        ],
                    }
                )
        return {"actions": actions}

    runner.client.feature(
        _internal_client_types.LIST_WORKSPACE_ACTIONS,
        handle_list_workspace_actions,
    )

    async def handle_elicit(
        params: _internal_client_types.ElicitParams,
    ) -> _internal_client_types.ElicitResult:
        """Serve ``finecode/elicit`` (ER → WM → the run's originating client).

        The addressee is resolved from the run the ER names, which is the run id
        the WM handed it at dispatch. A run with no recorded origin — one
        dispatched through a path that never held a client, or named by an ER
        too old to send one — is told at once that nobody could be asked, rather
        than waiting out a deadline for a client that was never listening.

        A run that fans out across the workspace resolves just as exactly: the
        nested dispatch inherits the calling run's connection, and the run it
        mints is bound to that same client, so which project the asking ER
        happens to serve never enters into it.
        """
        installed = elicitation_bridge.handlers()
        if installed is None:
            raise errors.InternalError(
                "This WM has no client-connection layer installed, so it cannot "
                "put a question to anyone."
            )
        origin = elicitation_bridge.originating_client_for_run(params.run_id)
        answer = await installed.elicit(
            message=params.message,
            options=list(params.options),
            default=params.default,
            timeout_sec=params.timeout_sec,
            run_writer_key=origin,
        )
        return _internal_client_types.ElicitResult(
            outcome=answer.get("outcome", "unavailable"),
            value=answer.get("value"),
        )

    runner.client.feature(
        _internal_client_types.ELICIT,
        handle_elicit,
    )


_STOP_TIMEOUT_SEC: typing.Final = 10


async def stop_extension_runner(
    runner: runner_client.ExtensionRunnerInfo,
    ws_context: context.WorkspaceContext,
) -> None:
    logger.trace(f"Trying to stop extension runner {runner.readable_id}")
    if runner.status in (
        runner_client.RunnerStatus.RUNNING,
        runner_client.RunnerStatus.REPAIRING,
    ):
        # A `BaseRunnerRequestException` means the shutdown RPC itself did not
        # come back — a timeout or a dead channel. There is no live RPC channel
        # to ask cooperatively, which is exactly the precondition
        # `force_kill()` documents, so it is force-killed directly rather than
        # sent an `exit` that cannot be answered. Any other failure is not
        # evidence the channel is dead, so it keeps the graceful path.
        channel_dead = False
        try:
            await _internal_client_api.shutdown(client=runner.client)
        except jsonrpc_client.BaseRunnerRequestException as error:
            channel_dead = True
            logger.warning(
                f"Extension runner {runner.readable_id} did not answer shutdown"
                f" ({error}); force-killing it"
            )
        except Exception as e:
            logger.error(f"Failed to shutdown {runner.readable_id}:")
            logger.exception(e)

        if channel_dead:
            runner.client.force_kill()
        else:
            await _internal_client_api.exit(client=runner.client)

            # `exit` only sends a notification; the OS process (and anything it is
            # still flushing, e.g. WAL files) may keep running briefly after this.
            # Wait for it to actually terminate so callers can safely remove its
            # venv/state directories right after this returns. The timeout is
            # passed into the thread itself (rather than wrapping an unbounded
            # `.wait()` in `asyncio.wait_for`) so a slow-to-stop runner doesn't
            # leak a blocked thread from the default executor.
            # Deliberately no force-kill fallback here: the ER already received
            # `exit` and may legitimately still be tearing down its own spawned
            # subprocesses (e.g. a package-manager invocation). Killing it mid
            # cleanup risks orphaning exactly the children a slower-but-graceful
            # exit would have reaped itself. `force_kill()` is only used where
            # there is no live RPC channel to ask cooperatively — a start-attempt
            # failure, `_start_runner`'s abandon path (any exit before `RUNNING`),
            # an INITIALIZING runner swept on WM shutdown, or a `shutdown` RPC
            # that went unanswered (the `channel_dead` branch above) — see
            # `_start_extension_runner_process` and `shutdown_service.on_shutdown`,
            # never as a timeout fallback here.
            stopped = await asyncio.to_thread(
                runner.client.server_process_stopped.wait, _STOP_TIMEOUT_SEC
            )
            if not stopped:
                logger.warning(
                    f"Extension runner {runner.readable_id} did not stop within"
                    f" {_STOP_TIMEOUT_SEC}s of exit"
                )

        logger.trace(f"Stopped extension runner {runner.readable_id}")
    else:
        logger.trace("Extension runner was not running")

    # Whatever the ER released gracefully on its way out, reclaim what it still
    # holds. A force-killed or crashed ER cannot release its own leases, and a
    # graceful one may not have released every run that was mid-flight when the
    # exit arrived — the budget must not leak slots permanently (ADR-0090).
    await ws_context.process_budget.reclaim_for_runner(runner.readable_id)


async def reap_failed_channel_runners(
    project_dir: Path, ws_context: context.WorkspaceContext
) -> list[str]:
    """Force-kill runners of *project_dir* whose RPC channel already failed.

    A runner whose channel is dead cannot be stopped cooperatively; the
    recovery-failure path uses this so it does not survive as an orphan. The
    process watcher still owns the EXITED transition (`on_exit`).
    """
    reaped: list[str] = []
    runners_by_env = ws_context.ws_projects_extension_runners.get(project_dir, {})
    for env_name, runner in runners_by_env.items():
        if runner.client is None or not runner.client.channel_failed:
            continue
        logger.warning(
            f"Reaping extension runner {runner.readable_id}: its RPC channel"
            " failed during configuration recovery"
        )
        runner.client.force_kill()
        await ws_context.process_budget.reclaim_for_runner(runner.readable_id)
        reaped.append(env_name)
    return reaped


def _warn_handlerless_actions(handlerless: dict[str, list[Path]]) -> None:
    for source, project_dirs in sorted(handlerless.items()):
        logger.warning(
            f"Action {source!r} has no handlers configured in {len(project_dirs)}"
            " project(s) — its metadata will not be resolved there."
        )
        logger.debug(
            f"Projects with no handlers for {source!r}: "
            + ", ".join(str(p) for p in project_dirs)
        )


async def start_runners_with_presets(
    projects: list[domain.Project],
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
    python_overrides: dict[str, str] | None = None,
    resolve_presets: bool = True,
) -> None:
    # start runners with presets in projects, resolve presets and read project actions
    # first start runner in 'dev_workspace' env to be able to resolve presets for
    # other envs (presets can be currently only in `dev_workspace` env)
    projects_to_start: list[domain.Project] = []
    initializing_runner_projects: list[
        tuple[domain.Project, runner_client.ExtensionRunnerInfo]
    ] = []
    coros = []

    for project in projects:
        project_status = project.status
        if project_status == domain.ProjectStatus.CONFIG_VALID:
            # first check whether runner doesn't exist yet to avoid duplicates
            project_runners = ws_context.ws_projects_extension_runners.get(
                project.dir_path, {}
            )
            project_dev_workspace_runner = project_runners.get("dev_workspace", None)
            start_new_runner = True
            if (
                project_dev_workspace_runner is not None
                and project_dev_workspace_runner.status
                in [
                    runner_client.RunnerStatus.INITIALIZING,
                    runner_client.RunnerStatus.RUNNING,
                    runner_client.RunnerStatus.REPAIRING,
                ]
            ):
                # start a new one only if:
                # - either there is no runner yet
                # or venv exist(=exclude `runner_client.RunnerStatus.NO_VENV`)
                #    and runner is not initializing or running already
                start_new_runner = False
                if (
                    project_dev_workspace_runner.status
                    == runner_client.RunnerStatus.INITIALIZING
                ):
                    # Runner started by a concurrent call — must wait before the second
                    # pass (reading configs) so that runner.client is set.
                    initializing_runner_projects.append(
                        (project, project_dev_workspace_runner)
                    )

            if start_new_runner:
                cmd_override = (python_overrides or {}).get("dev_workspace")
                coros.append(
                    _start_dev_workspace_runner(
                        project_def=project,
                        ws_context=ws_context,
                        cmd_override=cmd_override,
                    )
                )
                projects_to_start.append(project)
        elif project_status != domain.ProjectStatus.NO_FINECODE:
            raise RunnerFailedToStart(
                f"Project '{project.name}' has invalid configuration, status: {project_status.name}"
            )

    failed_names: list[str] = []

    if coros:
        # Use gather instead of TaskGroup so that a single project failure does not
        # cancel sibling startup tasks (which would leave them stuck in INITIALIZING).
        results = await asyncio.gather(*coros, return_exceptions=True)

        for project, result in zip(projects_to_start, results, strict=False):
            if isinstance(result, BaseException):
                if isinstance(
                    result,
                    (jsonrpc_client.BaseRunnerRequestException, RunnerFailedToStart),
                ):
                    msg = result.message
                else:
                    msg = repr(result)
                logger.error(
                    f"Runner for '{project.name}' ({project.dir_path}) failed to start: {msg}"
                )
                failed_names.append(project.name)

    # Wait for runners that were already INITIALIZING when we entered so that
    # runner.client is set before the second pass reads project configs.
    for project, runner in initializing_runner_projects:
        if not runner.initialized_event.is_set():
            await runner.initialized_event.wait()
        if runner.status != runner_client.RunnerStatus.RUNNING:
            logger.error(
                f"Runner for '{project.name}' ({project.dir_path}) did not reach RUNNING"
                f" state: {runner.status}"
            )
            failed_names.append(project.name)

    if failed_names:
        raise RunnerFailedToStart(
            f"Failed to start runner(s) for: {', '.join(failed_names)}. "
            f"See logs above for per-project details."
        )

    handlerless: dict[str, list[Path]] = {}
    try:
        for project in projects:
            if project.status != domain.ProjectStatus.CONFIG_VALID:
                continue

            try:
                await preset_resolution.read_project_config_with_py_presets(
                    project=project,
                    ws_context=ws_context,
                    resolve_presets=resolve_presets,
                )
                collected = collect_actions.collect_project(
                    project_path=project.dir_path, ws_context=ws_context
                )
            except config_models.ConfigurationError as exception:
                raise RunnerFailedToStart(
                    f"Reading project config with presets and collecting actions in {project.dir_path} failed: {exception.message}"
                ) from exception

            # Upgrade to ResolvedProject — presets are now resolved in the raw config
            resolved = domain.ResolvedProject.from_collected(collected)
            ws_context.ws_projects[project.dir_path] = resolved

            for action in resolved.actions:
                if not action.handlers:
                    handlerless.setdefault(action.source, []).append(project.dir_path)

            # update config of dev_workspace runner, the new config contains resolved presets
            dev_workspace_runner = ws_context.ws_projects_extension_runners[
                project.dir_path
            ]["dev_workspace"]
            handlers_to_init = (
                domain_helpers.collect_all_handlers_to_initialize(
                    resolved, "dev_workspace"
                )
                if initialize_all_handlers
                else None
            )
            await update_runner_config(
                runner=dev_workspace_runner,
                project=resolved,
                handlers_to_initialize=handlers_to_init,
                ws_context=ws_context,
            )
    finally:
        _warn_handlerless_actions(handlerless)


async def get_or_start_runners_with_presets(
    project_dir_path: Path, ws_context: context.WorkspaceContext
) -> runner_client.ExtensionRunnerInfo:
    # project is expected to have status `ProjectStatus.CONFIG_VALID`
    has_dev_workspace_runner = (
        "dev_workspace" in ws_context.ws_projects_extension_runners[project_dir_path]
    )
    if not has_dev_workspace_runner:
        project = ws_context.ws_projects[project_dir_path]
        await start_runners_with_presets([project], ws_context)
    dev_workspace_runner = ws_context.ws_projects_extension_runners[project_dir_path][
        "dev_workspace"
    ]
    if dev_workspace_runner.status == runner_client.RunnerStatus.RUNNING:
        return dev_workspace_runner
    elif dev_workspace_runner.status == runner_client.RunnerStatus.INITIALIZING:
        await dev_workspace_runner.initialized_event.wait()
        return dev_workspace_runner
    elif dev_workspace_runner.status == runner_client.RunnerStatus.REPAIRING:
        if dev_workspace_runner.repair_complete_event is not None:
            await dev_workspace_runner.repair_complete_event.wait()
        dev_workspace_runner = ws_context.ws_projects_extension_runners[
            project_dir_path
        ]["dev_workspace"]
        return dev_workspace_runner
    else:
        raise RunnerFailedToStart(
            f"Status of dev_workspace runner: {dev_workspace_runner.status}, logs: {dev_workspace_runner.logs_path}"
        )


async def _abandon_start(
    runner: runner_client.ExtensionRunnerInfo,
    ws_context: context.WorkspaceContext,
) -> None:
    """A start attempt that ended before RUNNING: leave no process and no waiter behind (ADR-0097)."""
    if runner.client is not None:
        runner.client.force_kill()
    if runner.status == runner_client.RunnerStatus.INITIALIZING:
        runner.status = runner_client.RunnerStatus.FAILED
    runner.initialized_event.set()
    await ws_context.process_budget.reclaim_for_runner(runner.readable_id)


async def start_runner(
    project_def: domain.Project,
    env_name: str,
    handlers_to_initialize: dict[str, list[str]] | None,
    ws_context: context.WorkspaceContext,
    debug: bool = False,
    cmd_override: str | None = None,
) -> runner_client.ExtensionRunnerInfo:
    with telemetry.er_startup_metrics(env_name):
        return await _start_runner(
            project_def=project_def,
            env_name=env_name,
            handlers_to_initialize=handlers_to_initialize,
            ws_context=ws_context,
            debug=debug,
            cmd_override=cmd_override,
        )


async def _start_runner(
    project_def: domain.Project,
    env_name: str,
    handlers_to_initialize: dict[str, list[str]] | None,
    ws_context: context.WorkspaceContext,
    debug: bool = False,
    cmd_override: str | None = None,
) -> runner_client.ExtensionRunnerInfo:
    # this function manages status of the runner and initialized event
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_def.dir_path,
        env_name=env_name,
        status=runner_client.RunnerStatus.INITIALIZING,
        initialized_event=asyncio.Event(),
        client=None,
        cmd_override=cmd_override,
    )
    save_runner_in_context(runner=runner, ws_context=ws_context)
    try:
        try:
            await _start_extension_runner_process(
                runner=runner, ws_context=ws_context, debug=debug
            )
        except asyncio.CancelledError:
            logger.warning(
                f"Startup of runner '{runner.readable_id}' was cancelled — marking as FAILED"
            )
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            raise

        try:
            await _init_lsp_client(runner=runner, project=project_def)
        except RunnerFailedToStart:
            runner.status = runner_client.RunnerStatus.FAILED
            await notify_project_changed(project_def)
            runner.initialized_event.set()
            raise

        try:
            runner_info = await _internal_client_api.get_runner_info(runner.client)
            if runner_info.log_file_path is not None:
                runner.log_file_path = Path(runner_info.log_file_path)
                logger.debug(
                    f"Runner {runner.readable_id} log file: {runner.log_file_path}"
                )
            else:
                logger.debug(f"Runner {runner.readable_id} returned no log file path")
        except Exception as e:
            logger.warning(f"Failed to get runner info for {runner.readable_id}: {e}")

        if (
            project_def.dir_path not in ws_context.ws_projects_raw_configs
            or not isinstance(project_def, domain.CollectedProject)
        ):
            try:
                await preset_resolution.read_project_config_with_py_presets(
                    project=project_def, ws_context=ws_context
                )
                collect_actions.collect_project(
                    project_path=project_def.dir_path, ws_context=ws_context
                )
            except config_models.ConfigurationError as exception:
                runner.status = runner_client.RunnerStatus.FAILED
                runner.initialized_event.set()
                await notify_project_changed(project_def)
                raise RunnerFailedToStart(
                    f"Found problem in configuration of {project_def.dir_path}: {exception.message}"
                ) from exception

        # Re-fetch from context — may now be CollectedProject if collection just happened
        current_project_def = ws_context.ws_projects[project_def.dir_path]
        if isinstance(current_project_def, domain.CollectedProject):
            # update runner config if project actions are already known, otherwise it will
            # be done as separate step
            await update_runner_config(
                runner=runner,
                project=current_project_def,
                handlers_to_initialize=handlers_to_initialize,
                ws_context=ws_context,
            )

        await _finish_runner_init(
            runner=runner, project=project_def, ws_context=ws_context
        )
    except BaseException:
        await _abandon_start(runner, ws_context)
        raise

    runner.status = runner_client.RunnerStatus.RUNNING
    telemetry.er_active_inc(runner.env_name)
    await notify_project_changed(project_def)
    runner.initialized_event.set()

    # A runner that starts while a client is subscribed to logs must begin
    # forwarding immediately (ADR-0049 Phase 2); no-op when nobody is watching.
    await wm_bridge.handlers().push_er_forwarding_to_runner(runner)

    return runner


async def _wait_for_runner_ready(
    runner: runner_client.ExtensionRunnerInfo,
    env_name: str,
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
) -> runner_client.ExtensionRunnerInfo:
    """Wait until *runner* reaches RUNNING, following INITIALIZING and REPAIRING transitions.

    Repair replaces the runner object in context, so the function re-fetches and loops
    rather than checking status once — this handles REPAIRING → INITIALIZING → RUNNING
    chains that the old one-shot if/elif missed.
    """
    while True:
        match runner.status:
            case runner_client.RunnerStatus.RUNNING:
                return runner
            case runner_client.RunnerStatus.INITIALIZING:
                logger.trace(
                    f"Runner {runner.readable_id} is initializing, wait for it"
                )
                await runner.initialized_event.wait()
                # status is now RUNNING or a terminal state — loop re-checks
            case runner_client.RunnerStatus.REPAIRING:
                logger.trace(
                    f"Runner {runner.readable_id} is being repaired, wait for it"
                )
                if runner.repair_complete_event is None:
                    raise RunnerFailedToStart(
                        f"Runner {env_name} in project {project_def.dir_path} is REPAIRING"
                        " but repair_complete_event is not set"
                    )
                await runner.repair_complete_event.wait()
                # Repair replaces the runner object in context — re-fetch and loop.
                runner = ws_context.ws_projects_extension_runners.get(
                    project_def.dir_path, {}
                ).get(env_name, runner)
            case _:
                raise RunnerFailedToStart(
                    f"Runner {env_name} in project {project_def.dir_path} is not running."
                    f" Status: {runner.status}"
                )


async def get_or_start_runner(
    project_def: domain.Project,
    env_name: str,
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
    action_names_to_initialize: list[str] | None = None,
    cmd_override: str | None = None,
) -> runner_client.ExtensionRunnerInfo:
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[project_def.dir_path]
        runner = runners_by_env[env_name]
        logger.trace(f"Runner {runner.readable_id} found")
    except KeyError:
        logger.trace(
            f"Runner for env {env_name} in {project_def.dir_path} not found, start one"
        )
        if initialize_all_handlers:
            handlers_to_initialize = domain_helpers.collect_all_handlers_to_initialize(
                project_def, env_name
            )
        elif action_names_to_initialize is not None:
            handlers_to_initialize = (
                domain_helpers.collect_handlers_to_initialize_for_actions(
                    project_def, env_name, action_names_to_initialize
                )
            )
        else:
            handlers_to_initialize = None
        runner = await start_runner(
            project_def=project_def,
            env_name=env_name,
            handlers_to_initialize=handlers_to_initialize,
            ws_context=ws_context,
            cmd_override=cmd_override,
        )

    return await _wait_for_runner_ready(
        runner=runner, env_name=env_name, project_def=project_def, ws_context=ws_context
    )


async def _start_dev_workspace_runner(
    project_def: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
    cmd_override: str | None = None,
) -> runner_client.ExtensionRunnerInfo:
    return await get_or_start_runner(
        project_def=project_def,
        env_name="dev_workspace",
        ws_context=ws_context,
        cmd_override=cmd_override,
    )


async def _init_lsp_client(
    runner: runner_client.ExtensionRunnerInfo, project: domain.Project
) -> None:
    try:
        await _internal_client_api.initialize(
            client=runner.client,
            client_process_id=os.getpid(),
            client_name="FineCode_WorkspaceManager",
            client_version="0.1.0",
            client_workspace_dir=runner.working_dir_path,
        )
    except jsonrpc_client.BaseRunnerRequestException as exception:
        raise RunnerFailedToStart(
            f"Runner failed to initialize: {exception.message}"
        ) from exception

    try:
        await _internal_client_api.notify_initialized(runner.client)
    except Exception as exception:
        logger.error(f"Failed to notify runner about initialization: {exception}")
        logger.exception(exception)
        raise RunnerFailedToStart(
            f"Runner failed to notify about initialization: {exception}"
        ) from exception

    logger.debug(f"LSP Client for initialized: {runner.readable_id}")


def _propagate_action_meta(
    resolved: domain.Action,
    source_project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> None:
    """Copy class-level metadata from a just-resolved action to every other
    project that registers the same action class.
    """
    for project in ws_context.ws_projects.values():
        if project is source_project or not isinstance(
            project, domain.CollectedProject
        ):
            continue
        for action in project.actions:
            if action is resolved:
                continue
            if action.source != resolved.source and (
                resolved.canonical_source is None
                or action.canonical_source != resolved.canonical_source
            ):
                continue
            if action.canonical_source is None:
                action.canonical_source = resolved.canonical_source
            action.scope = resolved.scope
            action.runs_concurrently = resolved.runs_concurrently
            if action.parent_action_source is None:
                action.parent_action_source = resolved.parent_action_source
            if action.language is None:
                action.language = resolved.language
            if action.file_loc is None:
                action.file_loc = resolved.file_loc


async def update_runner_config(
    runner: runner_client.ExtensionRunnerInfo,
    project: domain.CollectedProject,
    handlers_to_initialize: dict[str, list[str]] | None,
    ws_context: context.WorkspaceContext,
) -> None:
    _default_env_config = domain.EnvConfig(
        runner_config=domain.RunnerConfig(debug=False)
    )
    env_config = project.env_configs.get(runner.env_name, _default_env_config)
    actions_for_runner = [
        action
        for action in project.actions
        if any(h.env == runner.env_name for h in action.handlers)
    ]
    config = runner_client.RunnerConfig(
        actions=actions_for_runner,
        action_handler_configs=project.action_handler_configs,
        services=project.services,
        service_config_overrides=ws_context.service_config_overrides,
        handlers_to_initialize=handlers_to_initialize,
        logging=env_config.runner_config.logging,
        telemetry=runner_client.ErTelemetryConfig(
            otlp_endpoint=ws_context.otlp_endpoint,
        ),
    )
    try:
        await runner_client.update_config(runner, project.def_path, config)
    except jsonrpc_client.BaseRunnerRequestException as exception:
        runner.status = runner_client.RunnerStatus.FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        stale_env = (
            isinstance(exception, jsonrpc_client.ErrorOnRequest)
            and exception.error.code == _ENV_REINSTALL_NEEDED_ERROR_CODE
        )
        message = f"Runner failed to update config: {exception.message}"
        if stale_env:
            raise EnvironmentOutOfDateError(
                message, env_name=runner.env_name
            ) from exception
        raise RunnerFailedToStart(message) from exception

    try:
        action_meta_response = await runner_client.resolve_action_meta(runner)
    except Exception as exc:
        logger.warning(
            f"Failed to resolve action meta for runner {runner.readable_id}: {exc}"
        )
        action_meta_response = {}

    action_meta: dict[str, dict] = action_meta_response.get("actions", {})
    handler_locations: dict[str, str | None] = action_meta_response.get(
        "handlerLocations", {}
    )

    actions_without_meta: list[str] = []
    for action in project.actions:
        meta = action_meta.get(action.source)
        if meta is None:
            actions_without_meta.append(action.source)
        else:
            # Use the first runner that can successfully import an action to set its
            # canonical_source.  Multiple runners for the same project should agree on
            # canonical paths, so "first wins" is safe.
            if action.canonical_source is None:
                action.canonical_source = meta["canonical_source"]

            action.runs_concurrently = meta["runs_concurrently"]
            action.scope = domain.ActionScope(meta["scope"])
            if action.parent_action_source is None:
                action.parent_action_source = meta.get("parentActionSource")
            if action.language is None:
                action.language = meta.get("language")
            if action.file_loc is None:
                action.file_loc = meta.get("fileLoc")

            # Scope and other class-level attributes are identical across every
            # project that registers the same action class.  Propagate immediately
            # so the dispatch layer sees the correct scope even before each
            # project's own ER has started.
            _propagate_action_meta(action, project, ws_context)

        for handler in action.handlers:
            if handler.env != runner.env_name:
                continue
            if handler.file_loc is None and handler.source in handler_locations:
                handler.file_loc = handler_locations[handler.source]

    ws_context.ws_action_schemas.pop(project.dir_path, None)
    logger.debug(f"Updated config of runner {runner.readable_id}")


async def _finish_runner_init(
    runner: runner_client.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # TODO: save per runner only during initialization. But where to get data from
    #       in case of runner restart?
    await send_opened_files(
        runner=runner, opened_files=list(ws_context.opened_documents.values())
    )


def save_runner_in_context(
    runner: runner_client.ExtensionRunnerInfo, ws_context: context.WorkspaceContext
) -> None:
    if runner.working_dir_path not in ws_context.ws_projects_extension_runners:
        ws_context.ws_projects_extension_runners[runner.working_dir_path] = {}
    ws_context.ws_projects_extension_runners[runner.working_dir_path][
        runner.env_name
    ] = runner


async def send_opened_files(
    runner: runner_client.ExtensionRunnerInfo,
    opened_files: list[domain.TextDocumentInfo],
):
    files_for_runner: list[domain.TextDocumentInfo] = []
    for opened_file_info in opened_files:
        file_path = Path(opened_file_info.uri.replace("file://", ""))
        if not file_path.is_relative_to(runner.working_dir_path):
            continue
        else:
            files_for_runner.append(opened_file_info)

    try:
        async with asyncio.TaskGroup() as tg:
            for file_info in files_for_runner:
                tg.create_task(
                    runner_client.notify_document_did_open(
                        runner=runner,
                        document_info=file_info,
                    )
                )
    except ExceptionGroup as eg:
        logger.error(f"Error while sending opened document: {eg.exceptions}")


VERSION_CHECK_TIMEOUTS_SEC: tuple[float, ...] = (5.0, 30.0)
"""Successive timeouts for the ER version check of an env.

The check starts a Python interpreter, so when many envs are checked at once
(prepare-envs checks every project concurrently) a healthy env can miss a short
deadline on a loaded machine. A timeout is retried with a longer deadline
before the env is declared invalid, because the caller answers "invalid" by
deleting and recreating it.
"""

_OUTPUT_TAIL_CHARS = 500


@dataclasses.dataclass(frozen=True)
class RunnerEnvCheck:
    """Outcome of :func:`check_runner`; ``reason`` says why an env is invalid."""

    valid: bool
    reason: str = ""


async def _run_version_check(
    python_cmd: str, runner_dir: Path, timeout: float
) -> tuple[int, str, str] | None:
    """Run the ER version command; ``None`` if it did not finish within ``timeout``."""
    process = await asyncio.create_subprocess_exec(
        python_cmd,
        "-m",
        "finecode_extension_runner.cli",
        "version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=runner_dir,
    )
    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError:
        # Reap it: an abandoned check keeps competing for the CPU whose
        # shortage made it slow in the first place.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    assert process.returncode is not None
    return process.returncode, raw_stdout.decode(), raw_stderr.decode()


async def check_runner(runner_dir: Path, env_name: str) -> RunnerEnvCheck:
    """Check that env ``env_name`` of ``runner_dir`` can start an extension runner.

    Every invalid outcome carries its reason, so the caller's log line says why
    an env is about to be recreated without needing debug logging.
    """
    try:
        python_cmd = finecode_cmd.get_python_cmd(runner_dir, env_name)
    except ValueError as exception:
        return RunnerEnvCheck(valid=False, reason=str(exception))

    # get version of extension runner. If it works and we get valid
    # value, assume extension runner works correctly
    logger.debug(f"Check ER version of env '{env_name}' in {runner_dir}")
    result: tuple[int, str, str] | None = None
    for attempt, timeout in enumerate(VERSION_CHECK_TIMEOUTS_SEC):
        result = await _run_version_check(python_cmd, runner_dir, timeout)
        if result is not None:
            break
        if attempt + 1 < len(VERSION_CHECK_TIMEOUTS_SEC):
            logger.warning(
                f"ER version check of env '{env_name}' in {runner_dir} did not"
                f" finish within {timeout:g}s; retrying with"
                f" {VERSION_CHECK_TIMEOUTS_SEC[attempt + 1]:g}s. A slow check"
                " usually means the machine is overloaded, not that the env is"
                " broken."
            )
    if result is None:
        timeouts = ", ".join(f"{t:g}s" for t in VERSION_CHECK_TIMEOUTS_SEC)
        return RunnerEnvCheck(
            valid=False,
            reason=f"ER version check did not finish within any of {timeouts}",
        )

    returncode, stdout, stderr = result
    if returncode != 0:
        return RunnerEnvCheck(
            valid=False,
            reason=(
                f"ER version check exited with code {returncode}:"
                f" {stderr.strip()[-_OUTPUT_TAIL_CHARS:]}"
            ),
        )
    if "FineCode Extension Runner " not in stdout:
        return RunnerEnvCheck(
            valid=False,
            reason=(
                "ER version check printed unexpected output:"
                f" {stdout.strip()[-_OUTPUT_TAIL_CHARS:]!r}"
            ),
        )
    return RunnerEnvCheck(valid=True)


ENV_CHECK_BUDGET_OWNER = "wm:env-version-check"
"""Process-budget owner id for the WM's own env version checks (not an ER)."""


async def check_runner_within_budget(
    ws_context: context.WorkspaceContext, runner_dir: Path, env_name: str
) -> RunnerEnvCheck:
    """:func:`check_runner`, holding one process-budget work slot for its interpreter.

    prepare-envs checks every project's env at once. Unbounded, dozens of
    interpreters start together, overload the machine the WM shares, and the
    checks time out on healthy envs. The lease is non-nested: the WM holds no
    other slot while it waits, so waiting cannot deadlock (ADR-0090).
    """
    lease = await ws_context.process_budget.lease(
        runner_id=ENV_CHECK_BUDGET_OWNER, requested=1
    )
    try:
        return await check_runner(runner_dir=runner_dir, env_name=env_name)
    finally:
        await ws_context.process_budget.release(lease.lease_id)


async def remove_runner_env(runner_dir: Path, env_name: str) -> None:
    venv_dir_path = finecode_cmd.get_venv_dir_path(
        project_path=runner_dir, env_name=env_name
    )
    if venv_dir_path.exists():
        logger.debug(f"Remove venv {venv_dir_path}")
        # A venv holds thousands of files: deleting it on the loop blocks every
        # RPC the WM owes its clients for seconds, and prepare-envs may delete
        # dozens at once.
        await asyncio.to_thread(shutil.rmtree, venv_dir_path)


async def restart_extension_runners(
    runner_working_dir_path: Path, ws_context: context.WorkspaceContext
) -> None:
    """Restart every runner of a project.

    Raises:
        RunnerNotFoundError: the workspace has no runners for that project.
    """
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError as exception:
        raise errors.RunnerNotFoundError(
            f"Cannot find runner for {runner_working_dir_path}"
        ) from exception

    # TODO: parallel?
    for runner in runners_by_env.values():
        await restart_extension_runner(
            runner_working_dir_path=runner.working_dir_path,
            env_name=runner.env_name,
            ws_context=ws_context,
        )


async def restart_extension_runner(
    runner_working_dir_path: Path,
    env_name: str,
    ws_context: context.WorkspaceContext,
    debug: bool = False,
) -> None:
    """Restart a single runner of a project.

    Raises:
        RunnerNotFoundError: the workspace has no runner for that project and env.
        RunnerFailedToStart: the runner was stopped but did not come back up.
    """
    # TODO: reload config?
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError as exception:
        raise errors.RunnerNotFoundError(
            f"Cannot find runner for {runner_working_dir_path}"
        ) from exception

    try:
        runner = runners_by_env[env_name]
    except KeyError as exception:
        raise errors.RunnerNotFoundError(
            f"Cannot find runner for env {env_name} in {runner_working_dir_path}"
        ) from exception

    await stop_extension_runner(runner, ws_context)

    project_def = ws_context.ws_projects[runner.working_dir_path]

    await start_runner(
        project_def=project_def,
        env_name=runner.env_name,
        handlers_to_initialize=None,
        ws_context=ws_context,
        debug=debug,
    )
