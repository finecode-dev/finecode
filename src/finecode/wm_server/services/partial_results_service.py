"""Helper for running actions that produce streaming partial results.

It is intentionally small and
only encapsulates the orchestration logic; it does **not** perform any I/O
with client sockets.  The request handler in ``wm_server.py`` will take the
async iterator produced here and write notifications back to the caller.
"""
from __future__ import annotations

import asyncio

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


class PartialResultsStream:
    """Asynchronous stream of partial values with final-result storage.

    Instances support ``async for`` iteration; values appended by the producer
    are yielded to the consumer until :meth:`set_final` is called and the
    internal queue is drained.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[domain.PartialResultRawValue] = asyncio.Queue()
        self._final: dict | None = None
        self._done = asyncio.Event()

    def put(self, value: domain.PartialResultRawValue) -> None:
        self._queue.put_nowait(value)

    def set_final(self, result: dict) -> None:
        self._final = result
        self._done.set()

    async def __aiter__(self):
        # keep yielding until done and queue drained
        while True:
            if self._done.is_set() and self._queue.empty():
                break
            yield await self._queue.get()

    async def final_result(self) -> dict:
        await self._done.wait()
        return self._final or {}


async def run_action_with_partial_results(
    action_name: str,
    project_name: str,
    params: dict,
    partial_result_token: str | int,
    run_trigger: RunActionTrigger,
    dev_env: DevEnv,
    ws_context: context.WorkspaceContext,
    result_formats: list[str] | None = None,
) -> PartialResultsStream:
    """Run an action and return a stream of partial values.

    If ``project_name`` is the empty string the action will be executed in all
    projects that declare it; otherwise it is run only in the named project.

    The returned :class:`PartialResultsStream` can be iterated to receive
    ``domain.PartialResultRawValue`` objects.  Once execution completes the
    caller should call :meth:`PartialResultsStream.final_result` to obtain the
    aggregated result equivalent to what ``actions/run`` would return.
    """

    # determine target project(s) — only CollectedProject instances have actions
    projects: list[domain.CollectedProject]
    if project_name:
        projects = [
            p for p in ws_context.ws_projects.values()
            if p.name == project_name and isinstance(p, domain.CollectedProject)
        ]
        if not projects:
            raise ValueError(f"Project '{project_name}' not found")
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
    final_results: list[dict] = []
    return_codes: list[int] = []
    runners_used: list[runner_client.ExtensionRunnerInfo] = []

    async def run_one(project: domain.CollectedProject) -> None:
        logger.info(f"partial_results: run_one start project={project.name} action={action_name} token={partial_result_token}")
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
        ) as ctx:
            partial_count = 0
            async for value in ctx:
                partial_count += 1
                value_preview = str(value)[:200] if value else "None"
                logger.trace(f"partial_results: got partial #{partial_count} from runner for project={project.name}: {value_preview}")
                result_by_format: dict[str, domain.PartialResultRawValue] = {}
                if "json" in requested_formats:
                    result_by_format["json"] = value
                stream.put({"result_by_format": result_by_format})
            logger.trace(f"partial_results: partial iteration done for project={project.name}, got {partial_count} partials")

        # Responses collected by the context manager from runner tasks
        for resp in ctx.responses:
            json_result = resp.json()
            logger.trace(f"partial_results: final result for project={project.name}: return_code={resp.return_code}, keys={list(json_result.keys()) if isinstance(json_result, dict) else type(json_result)}")
            final_results.append(json_result)
            return_codes.append(resp.return_code)

            # If the runner sent no partial results (collected everything internally
            # and returned it all as the final response), emit the final result as a
            # partial result so the client still receives streaming updates.
            if partial_count == 0 and json_result:
                result_by_format: dict[str, domain.PartialResultRawValue] = {}
                if "json" in requested_formats:
                    result_by_format["json"] = json_result
                logger.trace(f"partial_results: no partials received for project={project.name}, emitting final result as partial")
                stream.put({"result_by_format": result_by_format})

        # Collect a runner from this project to use for cross-project result merging.
        action = next((a for a in project.actions if a.name == action_name), None)
        if action and action.handlers:
            env_name = action.handlers[0].env
            runner = ws_context.ws_projects_extension_runners.get(project.dir_path, {}).get(env_name)
            if runner is not None:
                runners_used.append(runner)

    async with asyncio.TaskGroup() as tg:
        for proj in projects:
            tg.create_task(run_one(proj))

    if final_results and runners_used:
        aggregated = await runner_client.merge_results(runners_used[0], action_name, final_results)
    else:
        aggregated = {}
    logger.trace(f"partial_results: aggregated result keys={list(aggregated.keys()) if isinstance(aggregated, dict) else type(aggregated)}")
    final_result_by_format: dict[str, dict] = {}
    if "json" in requested_formats:
        final_result_by_format["json"] = aggregated
    stream.set_final({"result_by_format": final_result_by_format, "return_code": max(return_codes) if return_codes else 0})
    return stream
