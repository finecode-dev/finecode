"""Helper for running actions that produce streaming partial results.

It is intentionally small and
only encapsulates the orchestration logic; it does **not** perform any I/O
with client sockets.  The request handler in ``wm_server.py`` will take the
async iterator produced here and write notifications back to the caller.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import typing
import uuid

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.context import pick_workspace_root_dir
from finecode.wm_server.errors import ActionNotResolvableError
from finecode.wm_server.runner import elicitation_bridge, runner_client
from finecode.wm_server.services import project_resolution_service
from finecode.wm_server.services.run_service import (
    ActionRunFailed,
    DevEnv,
    RunActionTrigger,
    RunResultFormat,
    matrix_runner,
    matrix_streaming,
    run_with_partial_results,
    start_required_environments,
)
from finecode.wm_server.utils import stream_completion

_DONE_SENTINEL: typing.Final = object()


class StreamFinal(typing.TypedDict):
    """Final data carried by a completed direct-service run."""

    returnCode: int


class PartialResultsStream:
    """Owned stream of partial values with writer-handoff completion.

    ``partials`` and the optional ``progress`` are single-consumer
    completion streams. Intermediate relays forward each fence only after
    the preceding transformations/enqueues; final-sink iteration
    acknowledges a fence only after the consumer body (the writer handoff)
    resumes. :meth:`final_result` exposes the retained terminal outcome
    after consumers finish — a received runner error surfaces there and on
    context exit, never by cancelling the consumer.
    """

    def __init__(self, *, with_progress: bool) -> None:
        self._partials: stream_completion.CompletionStream[
            domain.PartialResultRawValue
        ] = stream_completion.CompletionStream()
        self._progress: (
            stream_completion.CompletionStream[domain.ProgressRawValue] | None
        ) = stream_completion.CompletionStream() if with_progress else None
        self._outcome: stream_completion.TerminalOutcome[StreamFinal] | None = None
        self._done = asyncio.Event()

    @property
    def progress_stream(
        self,
    ) -> stream_completion.CompletionStream[domain.ProgressRawValue] | None:
        return self._progress

    def _finish(self, outcome: stream_completion.TerminalOutcome[StreamFinal]) -> None:
        self._outcome = outcome
        self._partials.end()
        if self._progress is not None:
            self._progress.end()
        self._done.set()

    def _abort(self, error: BaseException) -> None:
        fault: Exception = (
            error
            if isinstance(error, Exception)
            else stream_completion.PipelineFailure(error)
        )
        self._partials.abort(fault)
        if self._progress is not None:
            self._progress.abort(fault)
        self._outcome = stream_completion.RunInterrupted(error=error)
        self._done.set()

    def __aiter__(self) -> collections.abc.AsyncIterator[domain.PartialResultRawValue]:
        return self._partials.__aiter__()

    async def final_result(self) -> dict:
        await self._done.wait()
        outcome = self._outcome
        if isinstance(outcome, stream_completion.RunFailed):
            raise outcome.error
        if isinstance(outcome, stream_completion.RunInterrupted):
            raise outcome.error
        if isinstance(outcome, stream_completion.RunSucceeded):
            return {"returnCode": outcome.response["returnCode"]}
        raise ActionRunFailed("stream finished without a terminal outcome")


class ProgressStream:
    """Asynchronous stream of progress notifications (begin/report/end)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()

    def put(self, value: domain.ProgressRawValue) -> None:
        self._queue.put_nowait(value)

    def set_done(self) -> None:
        self._queue.put_nowait(_DONE_SENTINEL)

    async def __aiter__(self):
        while True:
            value = await self._queue.get()
            if value is _DONE_SENTINEL:
                break
            yield value


class ProgressAggregator:
    """Aggregates progress from multiple projects into a single stream.

    Each project reports progress independently. The aggregator collects
    ``total`` from each project's ``begin`` message, sums them, and remaps
    each project's percentage into a global percentage proportional to actual
    work. For single-project runs this degenerates to pass-through.
    """

    def __init__(
        self,
        num_projects: int,
        output: stream_completion.CompletionStream[domain.ProgressRawValue],
    ) -> None:
        self._num_projects = num_projects
        self._output = output
        self._per_project_total: dict[str, int | None] = {}
        self._per_project_completed: dict[str, int] = {}
        self._projects_ended: set[str] = set()
        self._began = False

    def on_progress(self, project_name: str, value: dict) -> None:
        progress_type = value.get("type")
        if progress_type == "begin":
            self._per_project_total[project_name] = value.get("total")
            self._per_project_completed[project_name] = 0
            if not self._began:
                self._began = True
                self._output.append(
                    {
                        "type": "begin",
                        "title": value.get("title", ""),
                        "percentage": 0,
                        "cancellable": value.get("cancellable", False),
                        "total": None,  # aggregated total not yet known
                    }
                )
        elif progress_type == "report":
            pct = value.get("percentage")
            proj_total = self._per_project_total.get(project_name)
            if proj_total is not None and pct is not None:
                self._per_project_completed[project_name] = int(proj_total * pct / 100)
            combined_total = sum(
                t for t in self._per_project_total.values() if t is not None
            )
            combined_done = sum(self._per_project_completed.values())
            combined_pct = (
                int(combined_done / combined_total * 100)
                if combined_total > 0
                else None
            )
            msg = value.get("message")
            if self._num_projects > 1 and msg:
                msg = f"{project_name}: {msg}"
            self._output.append(
                {
                    "type": "report",
                    "message": msg,
                    "percentage": combined_pct,
                }
            )
        elif progress_type == "end":
            proj_total = self._per_project_total.get(project_name)
            if proj_total is not None:
                self._per_project_completed[project_name] = proj_total
            self._projects_ended.add(project_name)
            if len(self._projects_ended) >= self._num_projects:
                self._output.append(
                    {
                        "type": "end",
                        "message": value.get("message"),
                    }
                )


@contextlib.asynccontextmanager
async def run_action_with_partial_results(
    action_name: str,
    project_path: str,
    params: dict,
    partial_result_token: str | int,
    run_trigger: RunActionTrigger,
    dev_env: DevEnv,
    ws_context: context.WorkspaceContext,
    origin: elicitation_bridge.RunDispatchOrigin | None,
    result_formats: list[str] | None = None,
    progress_token: str | int | None = None,
    selected_envs: set[str] | None = None,
) -> collections.abc.AsyncGenerator[PartialResultsStream]:
    """Run an action and yield a stream of partial values while it runs.

    If ``project_path`` is the empty string the action will be executed in all
    projects that declare it; otherwise it is run only in the project at that path.

    Routing is validated before yielding; the yielded
    :class:`PartialResultsStream` can be iterated to receive
    ``domain.PartialResultRawValue`` envelopes while the owned coordinator
    still runs. Once execution completes the caller should call
    :meth:`PartialResultsStream.final_result` to obtain the final completion
    payload (currently ``{"returnCode": int}``). A received runner error
    is captured so its prefix still completes, and is exposed after
    forwarding — never by cancelling the consumer.

    ``selected_envs`` (ADR-0103) restricts a matrixed action's
    fan-out to the given concrete env names, forwarded to every project's
    ``matrix_streaming.run_matrix_with_partial_results`` call; ``None`` (the
    default) runs the full declared axis.

    ``origin`` is who to ask if a handler elicits a choice while any of the
    fanned-out projects run (ADR-0082), forwarded unchanged to every project's
    dispatch; ``None`` is the honest answer for a run with no identifiable
    client.
    """

    # determine target project(s) — the gate resolves exactly who the action's
    # routing needs
    projects: list[domain.CollectedProject]
    if project_path:
        path = pathlib.Path(project_path)
        project = ws_context.ws_projects.get(path)
        if project is None:
            raise ValueError(f"Project '{project_path}' not found")
        try:
            project = (
                await project_resolution_service.ensure_projects_resolved(
                    [path], ws_context
                )
            ).require([path])[0]
        except project_resolution_service.ProjectResolutionFailed as exc:
            raise ActionRunFailed(exc.message) from exc
        # Mirrors the non-streaming actions/run guard in _helpers.py: a
        # workspace-scoped action's routing is the WM's decision (see the
        # `else` branch below), not the caller's — accepting an explicit
        # project here would silently narrow a workspace-wide action to one
        # runner instead of rejecting the combination.
        action = next((a for a in project.actions if a.name == action_name), None)
        if action is not None and action.canonical_source is None:
            # Scope is untrusted until the metadata resolves; a scope of None
            # would otherwise pass the check below silently.
            from finecode.wm_server.services.run_service import proxy_utils

            await proxy_utils.ensure_action_metadata(
                action, project, ws_context, selected_envs=selected_envs
            )
        if action is not None and action.scope == domain.ActionScope.WORKSPACE:
            raise ValueError(
                f"Action '{action_name}' is workspace-scoped; do not pass a project path."
            )
        projects = [project]
    else:
        from finecode.wm_server.services.run_service import proxy_utils

        # Root-first: a workspace-scoped root-hosted action runs in the
        # root alone; anything else resolves every project, and a failed
        # sibling fails the run loudly rather than being skipped.
        hosting = await project_resolution_service.resolve_hosting_projects(
            [action_name],
            "name",
            ws_context,
            selected_envs=selected_envs,
            run_options={"devEnv": dev_env.value},
        )
        if hosting.root_only:
            all_projects = list(hosting.outcome.resolved.values())
        else:
            try:
                resolved = hosting.outcome.require_all()
            except project_resolution_service.ProjectResolutionFailed as exc:
                raise ActionRunFailed(exc.message) from exc
            all_projects = [
                project
                for project in resolved.values()
                if any(a.name == action_name for a in project.actions)
            ]
        # A freshly resolved sibling's action has no metadata (the rebuild
        # drops it); ensure the first one before trusting its scope.
        first_action = next(
            (a for proj in all_projects for a in proj.actions if a.name == action_name),
            None,
        )
        if first_action is not None and first_action.canonical_source is None:
            first_project = next(
                proj for proj in all_projects if first_action in proj.actions
            )
            await proxy_utils.ensure_action_metadata(
                first_action, first_project, ws_context, selected_envs=selected_envs
            )
        if first_action is not None and first_action.scope is None:
            raise ActionNotResolvableError(
                f"Action '{action_name}' scope has not been resolved. "
                f"Metadata resolution must complete before dispatch."
            )
        if (
            first_action is not None
            and first_action.scope == domain.ActionScope.WORKSPACE
        ):
            workspace_root = pick_workspace_root_dir(ws_context)
            root_project = (
                ws_context.ws_projects.get(workspace_root) if workspace_root else None
            )
            if isinstance(root_project, domain.CollectedProject):
                projects = [root_project]
            else:
                logger.warning(
                    f"Workspace-scoped action '{action_name}': no project at workspace root, "
                    f"falling back to first declaring project"
                )
                projects = all_projects[:1] if all_projects else []
        else:
            projects = all_projects

    # start runners so that run_with_partial_results can attach
    await start_required_environments(
        {p.dir_path: [action_name] for p in projects},
        ws_context,
        initialize_all_handlers=True,
        selected_envs_by_project={p.dir_path: selected_envs for p in projects},
    )

    requested_formats = result_formats or ["json"]
    runner_formats = [
        RunResultFormat(fmt) for fmt in requested_formats if fmt in ("json", "string")
    ]

    stream = PartialResultsStream(with_progress=progress_token is not None)
    return_codes: list[int] = []
    failures: list[BaseException] = []

    # Set up progress forwarding if a progress token was provided.
    # Each project gets its own internal token; an aggregator combines them.
    # The aggregator emits directly into the owned progress stream so a fence
    # is forwarded after the values it bounds even when aggregation itself
    # produced no notification (e.g. a suppressed end).
    aggregator: ProgressAggregator | None = None
    progress_output = stream.progress_stream
    if progress_token is not None:
        assert progress_output is not None
        aggregator = ProgressAggregator(len(projects), progress_output)

    async def run_one(project: domain.CollectedProject) -> None:
        # Each project gets a unique internal progress token to avoid interleaving
        project_progress_token: str | None = None
        if progress_token is not None:
            project_progress_token = f"progress-{uuid.uuid4()}"

        try:
            action_def = next(
                (a for a in project.actions if a.name == action_name), None
            )
            if action_def is not None and matrix_runner.is_matrixed(action_def):
                logger.trace(
                    f"partial_results: run_one (matrixed) start project={project.name}"
                    f" action={action_name} token={partial_result_token}"
                )

                async def _on_partial(
                    interpreter_canonical: str, result_by_format: dict
                ) -> None:
                    stream._partials.append(
                        {
                            "project": str(project.dir_path),
                            "interpreter": interpreter_canonical,
                            "resultByFormat": result_by_format,
                        }
                    )

                async def _on_prefix(
                    fence: stream_completion.PrefixFence,
                ) -> None:
                    stream._partials.forward(fence)

                (
                    _combined_rbf,
                    matrix_return_code,
                ) = await matrix_streaming.run_matrix_with_partial_results(
                    project=project,
                    action=action_def,
                    action_name=action_name,
                    params=params,
                    result_formats=runner_formats,
                    partial_result_token=partial_result_token,
                    run_trigger=run_trigger,
                    dev_env=dev_env,
                    ws_context=ws_context,
                    merge_results=False,
                    on_partial=_on_partial,
                    on_prefix=_on_prefix,
                    selected_envs=selected_envs,
                    origin=origin,
                )
                return_codes.append(matrix_return_code)
                return

            partial_count = 0
            logger.trace(
                f"partial_results: run_one start project={project.name} action={action_name} token={partial_result_token}"
            )
            async with run_with_partial_results(
                action_name=action_name,
                params=params,
                partial_result_token=partial_result_token,
                project_dir_path=project.dir_path,
                run_trigger=run_trigger,
                dev_env=dev_env,
                ws_context=ws_context,
                initialize_all_handlers=True,
                result_formats=runner_formats,
                progress_token=project_progress_token,
                origin=origin,
            ) as ctx:

                async def _forward_partials() -> None:
                    nonlocal partial_count
                    async for event in ctx.partials.events():
                        if isinstance(event, stream_completion.PrefixFence):
                            # Forwarded after the preceding values; the final
                            # sink acknowledges it only after its own handoff.
                            stream._partials.forward(event)
                            continue
                        partial_count += 1
                        value_preview = (
                            str(event.value)[:200] if event.value else "None"
                        )
                        logger.trace(
                            f"partial_results: got partial #{partial_count} from runner for project={project.name}: {value_preview}"
                        )
                        # event.value is a result_by_format envelope {"json": ...}
                        # already filtered to requested formats by the ER.
                        stream._partials.append(
                            {
                                "project": str(project.dir_path),
                                "resultByFormat": event.value,
                            }
                        )
                    logger.trace(
                        f"partial_results: partial iteration done for project={project.name}, got {partial_count} partials"
                    )

                async def _forward_progress() -> None:
                    if ctx.progress is None or aggregator is None:
                        return
                    async for event in ctx.progress.events():
                        if isinstance(event, stream_completion.PrefixFence):
                            assert progress_output is not None
                            progress_output.forward(event)
                            continue
                        logger.trace(
                            f"progress: got type={event.value.get('type')} for project={project.name}"
                        )
                        aggregator.on_progress(project.name, event.value)

                async with asyncio.TaskGroup() as forward_tg:
                    forward_tg.create_task(_forward_partials())
                    forward_tg.create_task(_forward_progress())

            # Responses collected by the context manager from runner tasks
            for resp in ctx.responses:
                if resp.status == "streamed":
                    return_codes.append(resp.return_code)
                    continue
                if not resp.result_by_format:
                    raise runner_client.ActionRunFailed(
                        f"ER returned empty result with status '{resp.status}' for project={project.name}; "
                        "expected 'streamed' status when result_by_format is empty"
                    )

                logger.trace(
                    f"partial_results: final result for project={project.name}: return_code={resp.return_code}, keys={list(resp.result_by_format.keys())}"
                )
                return_codes.append(resp.return_code)

                # If the runner sent no partial results (collected everything internally
                # and returned it all as the final response), emit the final result as a
                # partial result so the client still receives streaming updates.
                if partial_count == 0 and resp.result_by_format:
                    logger.trace(
                        f"partial_results: no partials received for project={project.name}, emitting final result as partial"
                    )
                    stream._partials.append(
                        {
                            "project": str(project.dir_path),
                            "resultByFormat": resp.result_by_format,
                        }
                    )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failures.append(exc)

    async def _coordinate() -> None:
        # Ordinary runner failures are captured per project above and never
        # fail-fast their siblings; only owner cancellation escapes.
        try:
            async with asyncio.TaskGroup() as tg:
                for proj in projects:
                    tg.create_task(run_one(proj))
        except asyncio.CancelledError as exc:
            stream._abort(exc)
            raise
        if failures:
            errors: list[str] = []
            for exc in failures:
                if isinstance(exc, ActionRunFailed):
                    errors.append(exc.message)
                else:
                    errors.append(str(exc))
                    logger.exception(exc)
            stream._finish(
                stream_completion.RunFailed(ActionRunFailed("; ".join(errors)))
            )
        else:
            stream._finish(
                stream_completion.RunSucceeded(
                    StreamFinal(returnCode=max(return_codes) if return_codes else 0)
                )
            )

    async def _finalize(
        tracked: asyncio.Task[None],
    ) -> BaseException | None:
        cancelled_by_us = False
        if not tracked.done():
            _ = tracked.cancel("Consumer exited")
            cancelled_by_us = True
        outcomes = await asyncio.gather(tracked, return_exceptions=True)
        outcome = outcomes[0] if outcomes else None
        if cancelled_by_us and isinstance(outcome, asyncio.CancelledError):
            # Teardown cancel of a still-running coordinator carries no
            # signal itself. Real faults are already retained for the exit
            # path; a coordinator that already finished cancelled still
            # reports its own interruption below.
            return None
        return outcome if isinstance(outcome, BaseException) else None

    # The coordinator owns producer discovery and every producer lifetime;
    # the stream is yielded before it is awaited so sinks start while
    # producers are still running. A failed sink exits this context, which
    # cancels the coordinator rather than waiting for a runner response.
    coordinator = asyncio.ensure_future(_coordinate())
    consumer_error: BaseException | None = None
    try:
        yield stream
    except BaseException as exc:
        consumer_error = exc
    finalizer = asyncio.ensure_future(_finalize(coordinator))
    first_interruption: asyncio.CancelledError | None = None
    # Loop on the finalizer's state, not `while True`: when the finalizer
    # itself is cancelled (event-loop teardown cancels every task), shielding
    # it again raises at once without yielding.
    while not finalizer.done():
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError as exc:
            if first_interruption is None:
                first_interruption = exc
    if consumer_error is not None:
        raise consumer_error
    if first_interruption is not None:
        raise first_interruption
    coordinator_outcome = finalizer.result()
    if coordinator_outcome is not None:
        raise coordinator_outcome
    outcome = stream._outcome
    if isinstance(outcome, stream_completion.RunFailed):
        raise outcome.error
    # A RunInterrupted outcome here is only our own teardown abort after a
    # clean consumer exit; the coordinator join above already surfaced any
    # external interruption or failure.
