"""Helper for running actions that produce streaming partial results.

It is intentionally small and
only encapsulates the orchestration logic; it does **not** perform any I/O
with client sockets.  The request handler in ``wm_server.py`` will take the
async iterator produced here and write notifications back to the caller.
"""
from __future__ import annotations

import asyncio
import pathlib
import typing
import uuid

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client
from finecode.wm_server.services.run_service import (
    find_all_projects_with_action,
    run_with_partial_results,
    start_required_environments,
    RunActionTrigger,
    DevEnv,
    RunResultFormat,
)

_DONE_SENTINEL: typing.Final = object()


class PartialResultsStream:
    """Asynchronous stream of partial values with final-result storage.

    Instances support ``async for`` iteration; values appended by the producer
    are yielded to the consumer until :meth:`set_final` is called and the
    internal queue is drained.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._final: dict | None = None
        self._done = asyncio.Event()
        self.progress_stream: ProgressStream | None = None

    def put(self, value: domain.PartialResultRawValue) -> None:
        self._queue.put_nowait(value)

    def set_final(self, result: dict) -> None:
        self._final = result
        self._done.set()
        self._queue.put_nowait(_DONE_SENTINEL)

    async def __aiter__(self):
        while True:
            value = await self._queue.get()
            if value is _DONE_SENTINEL:
                break
            yield value

    async def final_result(self) -> dict:
        await self._done.wait()
        return self._final or {}


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

    def __init__(self, num_projects: int, output: ProgressStream) -> None:
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
                self._output.put({
                    "type": "begin",
                    "title": value.get("title", ""),
                    "percentage": 0,
                    "cancellable": value.get("cancellable", False),
                    "total": None,  # aggregated total not yet known
                })
        elif progress_type == "report":
            pct = value.get("percentage")
            proj_total = self._per_project_total.get(project_name)
            if proj_total is not None and pct is not None:
                self._per_project_completed[project_name] = int(proj_total * pct / 100)
            combined_total = sum(t for t in self._per_project_total.values() if t is not None)
            combined_done = sum(self._per_project_completed.values())
            combined_pct = int(combined_done / combined_total * 100) if combined_total > 0 else None
            msg = value.get("message")
            if self._num_projects > 1 and msg:
                msg = f"{project_name}: {msg}"
            self._output.put({
                "type": "report",
                "message": msg,
                "percentage": combined_pct,
            })
        elif progress_type == "end":
            proj_total = self._per_project_total.get(project_name)
            if proj_total is not None:
                self._per_project_completed[project_name] = proj_total
            self._projects_ended.add(project_name)
            if len(self._projects_ended) >= self._num_projects:
                self._output.put({
                    "type": "end",
                    "message": value.get("message"),
                })


async def run_action_with_partial_results(
    action_name: str,
    project_path: str,
    params: dict,
    partial_result_token: str | int,
    run_trigger: RunActionTrigger,
    dev_env: DevEnv,
    ws_context: context.WorkspaceContext,
    result_formats: list[str] | None = None,
    progress_token: str | int | None = None,
) -> PartialResultsStream:
    """Run an action and return a stream of partial values.

    If ``project_path`` is the empty string the action will be executed in all
    projects that declare it; otherwise it is run only in the project at that path.

    The returned :class:`PartialResultsStream` can be iterated to receive
    ``domain.PartialResultRawValue`` objects.  Once execution completes the
    caller should call :meth:`PartialResultsStream.final_result` to obtain the
    final completion payload (currently ``{"returnCode": int}``).
    """

    # determine target project(s) — only CollectedProject instances have actions
    projects: list[domain.CollectedProject]
    if project_path:
        project = ws_context.ws_projects.get(pathlib.Path(project_path))
        if project is None or not isinstance(project, domain.CollectedProject):
            raise ValueError(f"Project '{project_path}' not found")
        projects = [project]
    else:
        paths = find_all_projects_with_action(action_name, ws_context)
        projects = [
            p for path in paths
            if isinstance(p := ws_context.ws_projects[path], domain.CollectedProject)
        ]

    # start runners so that run_with_partial_results can attach
    await start_required_environments(
        {p.dir_path: [action_name] for p in projects},
        ws_context,
        initialize_all_handlers=True,
    )

    requested_formats = result_formats or ["json"]
    runner_formats = [RunResultFormat(fmt) for fmt in requested_formats if fmt in ("json", "string")]

    stream = PartialResultsStream()
    return_codes: list[int] = []

    # Set up progress forwarding if a progress token was provided.
    # Each project gets its own internal token; an aggregator combines them.
    progress_stream: ProgressStream | None = None
    aggregator: ProgressAggregator | None = None
    if progress_token is not None:
        progress_stream = ProgressStream()
        stream.progress_stream = progress_stream
        aggregator = ProgressAggregator(len(projects), progress_stream)

    async def run_one(project: domain.CollectedProject) -> None:
        partial_count = 0
        # Each project gets a unique internal progress token to avoid interleaving
        project_progress_token: str | None = None
        if progress_token is not None:
            project_progress_token = f"progress-{uuid.uuid4()}"

        logger.trace(f"partial_results: run_one start project={project.name} action={action_name} token={partial_result_token}")
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
        ) as ctx:
            async def _forward_partials() -> None:
                nonlocal partial_count
                async for value in ctx:
                    partial_count += 1
                    value_preview = str(value)[:200] if value else "None"
                    logger.trace(f"partial_results: got partial #{partial_count} from runner for project={project.name}: {value_preview}")
                    # TODO: partial results are single-format (bare value, no
                    # result_by_format envelope) so only JSON is supported here.
                    # To support other formats (e.g. "string"), the runner would
                    # need to send {format: value} pairs and get_partial_results /
                    # AsyncList would need to carry format information alongside
                    # each value.
                    result_by_format: dict[str, domain.PartialResultRawValue] = {}
                    if "json" in requested_formats:
                        result_by_format["json"] = value
                    stream.put({"resultByFormat": result_by_format})
                logger.trace(f"partial_results: partial iteration done for project={project.name}, got {partial_count} partials")

            async def _forward_progress() -> None:
                if ctx.progress is None or aggregator is None:
                    return
                async for value in ctx.progress:
                    logger.trace(f"progress: got type={value.get('type')} for project={project.name}")
                    aggregator.on_progress(project.name, value)

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

            json_result = resp.json()
            logger.trace(f"partial_results: final result for project={project.name}: return_code={resp.return_code}, keys={list(json_result.keys()) if isinstance(json_result, dict) else type(json_result)}")
            return_codes.append(resp.return_code)

            # If the runner sent no partial results (collected everything internally
            # and returned it all as the final response), emit the final result as a
            # partial result so the client still receives streaming updates.
            if partial_count == 0 and json_result:
                result_by_format: dict[str, domain.PartialResultRawValue] = {}
                if "json" in requested_formats:
                    result_by_format["json"] = json_result
                logger.trace(f"partial_results: no partials received for project={project.name}, emitting final result as partial")
                stream.put({"resultByFormat": result_by_format})

    async with asyncio.TaskGroup() as tg:
        for proj in projects:
            tg.create_task(run_one(proj))

    if progress_stream is not None:
        progress_stream.set_done()
    stream.set_final({"returnCode": max(return_codes) if return_codes else 0})
    return stream
