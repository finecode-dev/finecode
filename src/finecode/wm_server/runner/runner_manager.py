"""
API to manage ERs: start, stop, restart.
"""

import asyncio
import collections.abc
import concurrent.futures
import json
import os
import shutil
from pathlib import Path
import typing

from loguru import logger

from finecode import telemetry
from finecode.wm_server import context, domain, domain_helpers, errors
from finecode.wm_server.config import collect_actions, config_models, read_configs
from finecode.wm_server.runner import (
    runner_client,
    _internal_client_api,
    _internal_client_types,
    finecode_cmd
)
import finecode_jsonrpc as jsonrpc_client
from finecode_jsonrpc import _io_thread
project_changed_callback: (
    typing.Callable[[domain.Project], collections.abc.Coroutine[None, None, None]]
    | None
) = None
# get_document: typing.Callable[[], collections.abc.Coroutine] | None = None
apply_workspace_edit: typing.Callable[[], collections.abc.Coroutine] | None = None
start_debug_session: typing.Callable[[int], collections.abc.Coroutine] | None = None

# reexport
RunnerFailedToStart = jsonrpc_client.ServerFailedToStart
ServerConfigurationError = config_models.ConfigurationError


async def notify_project_changed(project: domain.Project) -> None:
    if project_changed_callback is not None:
        await project_changed_callback(project)


async def _apply_workspace_edit(
    params: _internal_client_types.ApplyWorkspaceEditParams,
):
    def map_change_object(change):
        return _internal_client_types.TextEdit(
            range=_internal_client_types.Range(
                start=_internal_client_types.Position(
                    line=change.range.start.line, character=change.range.start.character
                ),
                end=_internal_client_types.Position(
                    change.range.end.line, character=change.range.end.character
                ),
            ),
            new_text=change.newText,
        )

    converted_params = _internal_client_types.ApplyWorkspaceEditParams(
        edit=_internal_client_types.WorkspaceEdit(
            document_changes=[
                _internal_client_types.TextDocumentEdit(
                    text_document=_internal_client_types.OptionalVersionedTextDocumentIdentifier(
                        document_edit.text_document.uri
                    ),
                    edits=[map_change_object(change) for change in document_edit.edits],
                )
                for document_edit in params.edit.document_changes
                if isinstance(document_edit, _internal_client_types.TextDocumentEdit)
            ]
        )
    )
    return await apply_workspace_edit(converted_params)


async def _start_extension_runner_process(
    runner: runner_client.ExtensionRunnerInfo, ws_context: context.WorkspaceContext, debug: bool = False
) -> None:
    try:
        if runner.cmd_override:
            python_cmd = runner.cmd_override
        else:
            python_cmd = finecode_cmd.get_python_cmd(
                runner.working_dir_path, runner.env_name
            )
    except ValueError as exception:
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
    _default_env_config = domain.EnvConfig(runner_config=domain.RunnerConfig(debug=False))
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
    client = jsonrpc_client.JsonRpcClient(message_types=_internal_client_types.METHOD_TO_TYPES, readable_id=runner.readable_id, tracing=telemetry.JsonRpcTracingHooks())
    
    try:
        await client.start(server_cmd=f"{python_cmd} -m finecode_extension_runner.cli start {process_args_str}", working_dir_path=runner.working_dir_path, io_thread=ws_context.runner_io_thread, debug_port_future=debug_port_future, connect=not start_with_debug)
    except RunnerFailedToStart as exception:
        logger.error(f"Runner {runner.readable_id} failed to start: {exception.message}")
        runner.status = runner_client.RunnerStatus.FAILED
        runner.initialized_event.set()
        raise exception

    runner.client = client

    if start_with_debug:
        assert debug_port_future is not None

        # avoid blocking main thread?
        debug_async_future = asyncio.wrap_future(future=debug_port_future)
        try:
            await asyncio.wait_for(debug_async_future, timeout=30)
        except TimeoutError as exception:
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            raise RunnerFailedToStart(f"Failed to get debugger port in 30 seconds: {runner.readable_id}") from exception
        
        debug_port = debug_async_future.result()
        logger.info(f"debug port: {debug_port}")

        if start_debug_session is not None:
            debug_params = {
                "name": "Python: WM",
                "type": "debugpy",
                "request": "attach",
                "connect": {
                    "host": "localhost",
                    "port": debug_port
                },
                "justMyCode": False,
                # "logToFile": True,
            }
            await start_debug_session(debug_params)

        try:
            await client.connect_to_server(io_thread=ws_context.runner_io_thread, timeout=None)
        except Exception as exception: # TODO: analyze which can occur
            # TODO: analyze whether server process will always stop if connection
            logger.error(f"Runner {runner.readable_id} failed to connect to server: {exception}")
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
        logger.debug(f"Got progress from runner {runner.readable_id} for token: {params.token}")
        try:
            result_value = json.loads(params.value)
        except json.JSONDecodeError as exception:
            logger.error(f"Failed to decode partial result value json: {exception}")
            return

        # Distinguish progress notifications (begin/report/end) from partial results
        if isinstance(result_value, dict) and result_value.get("type") in ("begin", "report", "end"):
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

    async def on_er_user_message(params: dict) -> None:
        from finecode.wm_server import wm_server as _wm
        _wm._notify_all_clients(
            "server/userMessage",
            {"message": params.get("message", ""), "type": params.get("type", "WARNING")},
        )

    runner.client.feature(_internal_client_types.ER_USER_MESSAGE, on_er_user_message)

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
            raise errors.InternalError(f"Config of project '{project_def_path_str}' not found") from exception
        return _internal_client_types.GetProjectRawConfigResult(
            config=project_raw_config
        )

    runner.client.feature(
        _internal_client_types.PROJECT_RAW_CONFIG_GET,
        get_project_raw_config,
    )

    async def get_workspace_editable_packages(_params):
        return {
            "packages": {
                name: path.as_posix()
                for name, path in ws_context.ws_editable_packages.items()
            }
        }

    runner.client.feature(
        _internal_client_types.WORKSPACE_EDITABLE_PACKAGES_GET,
        get_workspace_editable_packages,
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

    async def handle_run_action_in_project(
        params: _internal_client_types.RunActionInProjectParams,
    ) -> _internal_client_types.RunActionInProjectResult:
        from finecode.wm_server.services.run_service import ProjectExecutor
        from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
        from finecode.wm_server.runner.runner_client import RunActionTrigger, DevEnv

        executor = ProjectExecutor(ws_context)

        if params.partial_result_token is not None:
            partial_count = 0
            try:
                async with executor.run_action_with_partial_results(
                    action_source=params.action_source,
                    params=params.payload,
                    project_path=runner.working_dir_path,
                    partial_result_token=params.partial_result_token,
                    run_trigger=RunActionTrigger(params.meta.trigger),
                    dev_env=DevEnv(params.meta.dev_env),
                    orchestration_depth=params.meta.orchestration_depth,
                    caller_kwargs=params.caller_kwargs,
                ) as ctx:
                    async for partial_raw in ctx:
                        partial_count += 1
                        runner.client.notify(
                            _internal_client_types.PROGRESS,
                            _internal_client_types.ProgressParams(
                                token=params.partial_result_token,
                                value=json.dumps(partial_raw),
                            ),
                        )
            except ActionRunFailed:
                raise

            if ctx.responses:
                final = ctx.responses[0]
                if final.status != "streamed":
                    final_json = final.result_by_format.get("json", {})
                    if partial_count == 0 and final_json:
                        runner.client.notify(
                            _internal_client_types.PROGRESS,
                            _internal_client_types.ProgressParams(
                                token=params.partial_result_token,
                                value=json.dumps(final_json),
                            ),
                        )
                return _internal_client_types.RunActionInProjectResult(
                    return_code=final.return_code,
                )
            return _internal_client_types.RunActionInProjectResult(
                return_code=0,
            )

        try:
            result = await executor.run_action(
                action_source=params.action_source,
                params=params.payload,
                project_path=runner.working_dir_path,
                run_trigger=RunActionTrigger(params.meta.trigger),
                dev_env=DevEnv(params.meta.dev_env),
                orchestration_depth=params.meta.orchestration_depth,
                caller_kwargs=params.caller_kwargs,
            )
        except ActionRunFailed:
            raise
        return _internal_client_types.RunActionInProjectResult(
            result=result.result_by_format.get("json", {}),
            return_code=result.return_code,
        )

    runner.client.feature(
        _internal_client_types.RUN_ACTION_IN_PROJECT,
        handle_run_action_in_project,
    )

    async def handle_run_action_in_workspace(
        params: _internal_client_types.RunActionInWorkspaceParams,
    ) -> dict:
        from finecode.wm_server.services.run_service import WorkspaceExecutor
        from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
        from finecode.wm_server.services.run_service.proxy_utils import find_all_projects_with_action
        from finecode.wm_server.runner.runner_client import RunActionTrigger, DevEnv

        run_trigger = RunActionTrigger(params.meta.trigger)
        dev_env = DevEnv(params.meta.dev_env)

        # Resolve action name from source via the runner's own project actions.
        # Use canonical_source (resolved by ER)
        project = ws_context.ws_projects.get(runner.working_dir_path)
        if not isinstance(project, domain.CollectedProject):
            raise errors.InternalError(f"Project {runner.working_dir_path} has no valid config")
        try:
            action_name = next(
                a.name for a in project.actions
                if a.canonical_source == params.action_source
            )
        except StopIteration:
            known = [
                f"{a.name}(source={a.source!r}, canonical={a.canonical_source!r})"
                for a in project.actions
            ]
            logger.info(
                f"handle_run_action_in_workspace: action_source={params.action_source!r} not found"
                f" in project {runner.working_dir_path}."
                f" Known actions ({len(known)}): {known}"
            )
            raise errors.ActionNotFoundError(
                f"No action with source '{params.action_source}' found in project {runner.working_dir_path}"
            )

        if params.project_paths:
            actions_by_project = {
                Path(p): [action_name] for p in params.project_paths
            }
        else:
            actions_by_project = {
                p: [action_name]
                for p in find_all_projects_with_action(action_name, ws_context)
            }

        executor = WorkspaceExecutor(ws_context)
        try:
            results = await executor.run_actions_in_projects(
                actions_by_project=actions_by_project,
                params=params.payload,
                run_trigger=run_trigger,
                dev_env=dev_env,
                orchestration_depth=params.meta.orchestration_depth,
                concurrently=params.concurrently,
            )
        except ActionRunFailed:
            raise
        return _internal_client_types.RunActionInWorkspaceResult(
            results_by_project={
                k.as_posix(): {
                    action: {
                        "result": resp.result_by_format.get("json"),
                        "status": resp.status,
                    }
                    for action, resp in v.items()
                }
                for k, v in results.items()
            }
        )

    runner.client.feature(
        _internal_client_types.RUN_ACTION_IN_WORKSPACE,
        handle_run_action_in_workspace,
    )

    async def handle_get_action_metadata(
        params: _internal_client_types.GetActionMetadataParams,
    ) -> _internal_client_types.GetActionMetadataResult:
        """Serve ``finecode/getActionMetadata`` from the WM metadata cache.

        Metadata is populated at runner startup via ``resolveActionMeta``.  When
        the action was not importable at startup (cache miss), auto-repair is
        triggered for the handler env: packages are reinstalled, the runner is
        restarted (which re-runs ``update_runner_config`` → ``resolveActionMeta``
        and repopulates the cache), and the result is read from cache again.

        Behavior contract (each numbered case is a distinct test scenario):

        1. Project not yet collected (not a CollectedProject):
           → ConfigurationError

        2. Action source not found in project config:
           → ActionNotFoundError

        3. Action found but has no declared handlers (allowed config state):
           → Returns null-fields result.

        4. Cache hit — metadata was resolved at startup:
           → Returns cached ``parent_action_source`` and ``language``.

        5. Cache miss — action not importable at startup:
           → Auto-repair (prepare-envs + runner restart) for the first handler
              env, then re-read cache.  If resolved after repair → return.
              If still missing → ActionNotResolvableError.

        6. Already marked unresolvable (fast-path for reconnected ERs):
           → Returns null-fields result immediately.
        """
        action_source: str = params.action_source

        # Case 6 fast-path: already determined unresolvable in this session —
        # return null immediately so reconnected ERs don't retrigger auto-repair.
        project_check = ws_context.ws_projects.get(runner.working_dir_path)
        if (
            isinstance(project_check, domain.CollectedProject)
            and action_source in project_check.unresolvable_metadata_sources
        ):
            return _internal_client_types.GetActionMetadataResult()

        # Case 1
        project = ws_context.ws_projects.get(runner.working_dir_path)
        if not isinstance(project, domain.CollectedProject):
            raise errors.ConfigurationError(
                f"Project '{runner.working_dir_path}' has no valid config"
            )

        # Case 2
        action = next(
            (
                a for a in project.actions
                if a.source == action_source or a.canonical_source == action_source
            ),
            None,
        )
        if action is None:
            raise errors.ActionNotFoundError(
                f"Action '{action_source}' not found in project '{runner.working_dir_path}'"
            )

        # Case 3: no handlers — cannot determine which env has the package
        if not action.handlers:
            return _internal_client_types.GetActionMetadataResult()

        # Case 4: cache hit — metadata already populated at startup
        if action.parent_action_source is not None or action.language is not None:
            return _internal_client_types.GetActionMetadataResult(
                parent_action_source=action.parent_action_source,
                language=action.language,
            )

        # Case 5: cache miss — action was not importable at startup.
        # Trigger auto-repair for the first handler env.  The restart re-runs
        # update_runner_config → resolveActionMeta, which repopulates the cache.
        # Guard against concurrent repairs using ws_context.pending_repair_events
        # (a dict keyed by (project_dir, env_name)) which is updated synchronously
        # — no await between the check and the insertion — so every concurrent
        # coroutine that reaches this point will see the event and wait.
        env_name = action.handlers[0].env
        if env_name in project.failed_repair_envs:
            project.unresolvable_metadata_sources.add(action_source)
            raise errors.ActionNotResolvableError(
                f"No runner for project '{runner.working_dir_path}' could resolve"
                f" action metadata for source '{action_source}'"
            )
        repair_key = (project.dir_path, env_name)
        if repair_key in ws_context.pending_repair_events:
            logger.debug(
                f"Repair already in progress for env {env_name!r}, waiting"
            )
            await ws_context.pending_repair_events[repair_key].wait()
        else:
            repair_event = asyncio.Event()
            # Store the event BEFORE any await so concurrent coroutines see it.
            ws_context.pending_repair_events[repair_key] = repair_event
            existing_runner = (
                ws_context.ws_projects_extension_runners
                .get(project.dir_path, {})
                .get(env_name)
            )
            if existing_runner is not None:
                existing_runner.status = runner_client.RunnerStatus.REPAIRING
                existing_runner.repair_complete_event = repair_event
            logger.info(
                f"Action '{action_source}' not importable in env {env_name!r}"
                f" — running auto-repair (prepare-envs + runner restart)"
            )
            try:
                from finecode.wm_server.services.prepare_envs_service import (
                    install_env_for_project,
                )
                await install_env_for_project(project, env_name, ws_context)
                logger.debug(
                    f"Auto-repair: env {env_name!r} installed, restarting runner"
                )
                await restart_extension_runner(
                    runner_working_dir_path=project.dir_path,
                    env_name=env_name,
                    ws_context=ws_context,
                )
            except Exception as exc:
                project.failed_repair_envs.add(env_name)
                logger.warning(f"Auto-repair failed for env {env_name!r}: {exc}")
            finally:
                ws_context.pending_repair_events.pop(repair_key, None)
                repair_event.set()

        # Re-read cache after repair (or after waiting for another coroutine's repair).
        if action.parent_action_source is not None or action.language is not None:
            logger.info(
                f"Auto-repair succeeded: resolved metadata for '{action_source}'"
                f" in env {env_name!r}"
            )
            return _internal_client_types.GetActionMetadataResult(
                parent_action_source=action.parent_action_source,
                language=action.language,
            )

        project.unresolvable_metadata_sources.add(action_source)
        raise errors.ActionNotResolvableError(
            f"No runner for project '{runner.working_dir_path}' could resolve"
            f" action metadata for source '{action_source}'"
            f" (tried env: {env_name!r})"
        )

    runner.client.feature(
        _internal_client_types.GET_ACTION_METADATA,
        handle_get_action_metadata,
    )


async def stop_extension_runner(runner: runner_client.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.readable_id}")
    if runner.status in (
        runner_client.RunnerStatus.RUNNING,
        runner_client.RunnerStatus.REPAIRING,
    ):
        try:
            await _internal_client_api.shutdown(client=runner.client)
        except Exception as e:
            logger.error(f"Failed to shutdown {runner.readable_id}:")
            logger.exception(e)

        await _internal_client_api.exit(client=runner.client)
        logger.trace(f"Stopped extension runner {runner.readable_id}")
    else:
        logger.trace("Extension runner was not running")


def stop_extension_runner_sync(runner: runner_client.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.readable_id}")
    if runner.status in (
        runner_client.RunnerStatus.RUNNING,
        runner_client.RunnerStatus.REPAIRING,
    ):
        try:
            _internal_client_api.shutdown_sync(client=runner.client)
        except Exception as e:
            logger.error(f"Failed to shutdown:")
            logger.exception(e)

        _internal_client_api.exit_sync(runner.client)
        logger.trace(f"Stopped extension runner {runner.readable_id}")
    else:
        logger.trace("Extension runner was not running")



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
    initializing_runner_projects: list[tuple[domain.Project, runner_client.ExtensionRunnerInfo]] = []
    coros = []

    for project in projects:
        project_status = project.status
        if project_status == domain.ProjectStatus.CONFIG_VALID:
            # first check whether runner doesn't exist yet to avoid duplicates
            project_runners = ws_context.ws_projects_extension_runners.get(
                project.dir_path, {}
            )
            project_dev_workspace_runner = project_runners.get(
                "dev_workspace", None
            )
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
                if project_dev_workspace_runner.status == runner_client.RunnerStatus.INITIALIZING:
                    # Runner started by a concurrent call — must wait before the second
                    # pass (reading configs) so that runner.client is set.
                    initializing_runner_projects.append((project, project_dev_workspace_runner))

            if start_new_runner:
                cmd_override = (python_overrides or {}).get("dev_workspace")
                coros.append(
                    _start_dev_workspace_runner(
                        project_def=project, ws_context=ws_context, cmd_override=cmd_override
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

        for project, result in zip(projects_to_start, results):
            if isinstance(result, BaseException):
                if isinstance(result, (jsonrpc_client.BaseRunnerRequestException, RunnerFailedToStart)):
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

    for project in projects:
        if project.status != domain.ProjectStatus.CONFIG_VALID:
            continue

        try:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=resolve_presets
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
                logger.warning(
                    f"Action {action.source!r} in {project.dir_path} has no handlers"
                    " configured — its metadata will not be resolved."
                )

        # update config of dev_workspace runner, the new config contains resolved presets
        dev_workspace_runner = ws_context.ws_projects_extension_runners[
            project.dir_path
        ]["dev_workspace"]
        handlers_to_init = (
            domain_helpers.collect_all_handlers_to_initialize(resolved, "dev_workspace")
            if initialize_all_handlers
            else None
        )
        await update_runner_config(
            runner=dev_workspace_runner,
            project=resolved,
            handlers_to_initialize=handlers_to_init,
            ws_context=ws_context,
        )


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
        dev_workspace_runner = ws_context.ws_projects_extension_runners[project_dir_path]["dev_workspace"]
        return dev_workspace_runner
    else:
        raise RunnerFailedToStart(
            f"Status of dev_workspace runner: {dev_workspace_runner.status}, logs: {dev_workspace_runner.logs_path}"
        )


async def start_runner(
    project_def: domain.Project, env_name: str, handlers_to_initialize: dict[str, list[str]] | None, ws_context: context.WorkspaceContext, debug: bool = False, cmd_override: str | None = None
) -> runner_client.ExtensionRunnerInfo:
    with telemetry.er_startup_metrics(env_name):
        return await _start_runner(project_def=project_def, env_name=env_name, handlers_to_initialize=handlers_to_initialize, ws_context=ws_context, debug=debug, cmd_override=cmd_override)


async def _start_runner(
    project_def: domain.Project, env_name: str, handlers_to_initialize: dict[str, list[str]] | None, ws_context: context.WorkspaceContext, debug: bool = False, cmd_override: str | None = None
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
        await _start_extension_runner_process(runner=runner, ws_context=ws_context, debug=debug)
    except asyncio.CancelledError:
        logger.warning(
            f"Startup of runner '{runner.readable_id}' was cancelled — marking as FAILED"
        )
        runner.status = runner_client.RunnerStatus.FAILED
        runner.initialized_event.set()
        raise

    try:
        await _init_lsp_client(runner=runner, project=project_def)
    except RunnerFailedToStart as exception:
        runner.status = runner_client.RunnerStatus.FAILED
        await notify_project_changed(project_def)
        runner.initialized_event.set()
        raise exception

    try:
        runner_info = await _internal_client_api.get_runner_info(runner.client)
        if runner_info.log_file_path is not None:
            runner.log_file_path = Path(runner_info.log_file_path)
            logger.debug(f"Runner {runner.readable_id} log file: {runner.log_file_path}")
        else:
            logger.debug(f"Runner {runner.readable_id} returned no log file path")
    except Exception as e:
        logger.warning(f"Failed to get runner info for {runner.readable_id}: {e}")

    if (
        project_def.dir_path not in ws_context.ws_projects_raw_configs
        or not isinstance(project_def, domain.CollectedProject)
    ):
        try:
            await read_configs.read_project_config(
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
        await update_runner_config(runner=runner, project=current_project_def, handlers_to_initialize=handlers_to_initialize, ws_context=ws_context)
    
    await _finish_runner_init(runner=runner, project=project_def, ws_context=ws_context)

    runner.status = runner_client.RunnerStatus.RUNNING
    telemetry.er_active_inc(runner.env_name)
    await notify_project_changed(project_def)
    runner.initialized_event.set()

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
                logger.trace(f"Runner {runner.readable_id} is initializing, wait for it")
                await runner.initialized_event.wait()
                # status is now RUNNING or a terminal state — loop re-checks
            case runner_client.RunnerStatus.REPAIRING:
                logger.trace(f"Runner {runner.readable_id} is being repaired, wait for it")
                if runner.repair_complete_event is None:
                    raise RunnerFailedToStart(
                        f"Runner {env_name} in project {project_def.dir_path} is REPAIRING"
                        " but repair_complete_event is not set"
                    )
                await runner.repair_complete_event.wait()
                # Repair replaces the runner object in context — re-fetch and loop.
                runner = (
                    ws_context.ws_projects_extension_runners
                    .get(project_def.dir_path, {})
                    .get(env_name, runner)
                )
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
            handlers_to_initialize = domain_helpers.collect_all_handlers_to_initialize(project_def, env_name)
        elif action_names_to_initialize is not None:
            handlers_to_initialize = domain_helpers.collect_handlers_to_initialize_for_actions(
                project_def, env_name, action_names_to_initialize
            )
        else:
            handlers_to_initialize = None
        runner = await start_runner(
            project_def=project_def, env_name=env_name, handlers_to_initialize=handlers_to_initialize, ws_context=ws_context, cmd_override=cmd_override
        )

    return await _wait_for_runner_ready(
        runner=runner, env_name=env_name, project_def=project_def, ws_context=ws_context
    )


async def _start_dev_workspace_runner(
    project_def: domain.CollectedProject, ws_context: context.WorkspaceContext, cmd_override: str | None = None
) -> runner_client.ExtensionRunnerInfo:
    return await get_or_start_runner(
        project_def=project_def, env_name="dev_workspace", ws_context=ws_context, cmd_override=cmd_override
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
            client_workspace_dir=runner.working_dir_path
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


async def update_runner_config(
    runner: runner_client.ExtensionRunnerInfo,
    project: domain.CollectedProject,
    handlers_to_initialize: dict[str, list[str]] | None,
    ws_context: context.WorkspaceContext,
) -> None:
    _default_env_config = domain.EnvConfig(runner_config=domain.RunnerConfig(debug=False))
    env_config = project.env_configs.get(runner.env_name, _default_env_config)
    actions_for_runner = [
        action for action in project.actions
        if any(h.env == runner.env_name for h in action.handlers)
    ]
    config = runner_client.RunnerConfig(
        actions=actions_for_runner,
        action_handler_configs=project.action_handler_configs,
        services=project.services,
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
        raise RunnerFailedToStart(
            f"Runner failed to update config: {exception.message}"
        ) from exception

    try:
        action_meta = await runner_client.resolve_action_meta(runner)
    except Exception as exc:
        logger.warning(f"Failed to resolve action meta for runner {runner.readable_id}: {exc}")
        action_meta = {}

    actions_without_meta: list[str] = []
    for action in project.actions:
        meta = action_meta.get(action.source)
        if meta is None:
            actions_without_meta.append(action.source)
            continue
        # Use the first runner that can successfully import an action to set its
        # canonical_source.  Multiple runners for the same project should agree on
        # canonical paths, so "first wins" is safe.
        if action.canonical_source is None:
            action.canonical_source = meta["canonical_source"]

        action.runs_concurrently = meta["runs_concurrently"]
        action.scope = domain.ActionScope(
            meta.get("scope", domain.ActionScope.PROJECT.value)
        )
        if action.parent_action_source is None:
            action.parent_action_source = meta.get("parentActionSource")
        if action.language is None:
            action.language = meta.get("language")

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
                        document_info=domain.TextDocumentInfo(
                            uri=file_info.uri, version=file_info.version
                        ),
                    )
                )
    except ExceptionGroup as eg:
        logger.error(f"Error while sending opened document: {eg.exceptions}")


async def check_runner(runner_dir: Path, env_name: str) -> bool:
    try:
        python_cmd = finecode_cmd.get_python_cmd(runner_dir, env_name)
    except ValueError:
        logger.debug(f"No venv for {env_name} of {runner_dir}")
        # no venv
        return False

    # get version of extension runner. If it works and we get valid
    # value, assume extension runner works correctly
    cmd = f"{python_cmd} -m finecode_extension_runner.cli version"
    logger.debug(f"Run '{cmd}' in {runner_dir}")
    async_subprocess = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=runner_dir,
    )
    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(
            async_subprocess.communicate(), timeout=5
        )
    except TimeoutError:
        logger.debug(f"Timeout 5 sec({runner_dir})")
        return False

    if async_subprocess.returncode != 0:
        logger.debug(
            f"Return code: {async_subprocess.returncode}, stderr: {raw_stderr.decode()}"
        )
        return False

    stdout = raw_stdout.decode()
    return "FineCode Extension Runner " in stdout


def remove_runner_env(runner_dir: Path, env_name: str) -> None:
    venv_dir_path = finecode_cmd.get_venv_dir_path(
        project_path=runner_dir, env_name=env_name
    )
    if venv_dir_path.exists():
        logger.debug(f"Remove venv {venv_dir_path}")
        shutil.rmtree(venv_dir_path)


async def restart_extension_runners(
    runner_working_dir_path: Path, ws_context: context.WorkspaceContext
) -> None:
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    # TODO: parallel?
    for runner in runners_by_env.values():
        await restart_extension_runner(runner_working_dir_path=runner.working_dir_path, env_name=runner.env_name, ws_context=ws_context)


async def restart_extension_runner(
    runner_working_dir_path: Path, env_name: str, ws_context: context.WorkspaceContext, debug: bool = False
) -> None:
    # TODO: reload config?
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    try:
        runner = runners_by_env[env_name]
    except KeyError:
        logger.error(f"Cannot find runner for env {env_name} in {runner_working_dir_path}")
        return

    await stop_extension_runner(runner)

    project_def = ws_context.ws_projects[runner.working_dir_path]

    # Invalidate the unresolvable-metadata cache for actions whose handlers run
    # in this env — the restart may follow a repair that fixed the package.
    if isinstance(project_def, domain.CollectedProject):
        stale = {
            src
            for a in project_def.actions
            if any(h.env == env_name for h in a.handlers)
            for src in (a.source, a.canonical_source)
            if src is not None
        }
        project_def.unresolvable_metadata_sources -= stale
        project_def.failed_repair_envs.discard(env_name)

    await start_runner(
        project_def=project_def,
        env_name=runner.env_name,
        handlers_to_initialize=None,
        ws_context=ws_context,
        debug=debug
    )
