"""Fills ``runner.run_dispatch_bridge``'s slot for ER-initiated action dispatch.

Imported for its ``install`` side effect by ``run_service/__init__.py``, the same way
``knowledge_service.py`` fills ``runner.knowledge_bridge``. See ADR-0072 for why the
runner reaches this code through a slot rather than importing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import typing
import uuid
from pathlib import Path

from loguru import logger

from finecode.wm_server import context, domain, errors
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    run_dispatch_bridge,
)
from finecode.wm_server.runner.runner_client import (
    DevEnv,
    ExtensionRunnerInfo,
    RunActionTrigger,
)
from finecode.wm_server.services import project_resolution_service
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.delegated_progress import (
    DELEGATED_PROGRESS_PENDING_LIMIT,
)
from finecode.wm_server.services.run_service.project_executor import ProjectExecutor
from finecode.wm_server.services.run_service.proxy_utils import (
    find_action_by_canonical_source,
    find_all_projects_with_action,
    find_subactions_for_parent,
)
from finecode.wm_server.services.run_service.workspace_executor import WorkspaceExecutor

_NEAREST_PROJECTS_IN_HINT = 3


@dataclasses.dataclass
class _DelegatedCallEntry:
    """Forwarding state of one streaming ``runActionInProject`` call.

    Keyed by the parent connection's object identity (``id`` of the runner's
    client, not the runner) plus the parent ``partialResultToken``, so two
    parent ERs using equal tokens never share an entry.

    A release only flips ``state`` to ``"released``: the forwarding pumps
    observe it and keep consuming their streams as discarding drains, so no
    consumed-but-unacknowledged fence is ever stranded by cancelling a pump
    mid-handoff (a stranded fence would stall the child run's drain and the
    child would never complete). The entry is popped from the table
    synchronously in the release, before the reply.
    """

    state: typing.Literal["registered", "forwarding", "released"]


_delegated_calls: dict[tuple[int, int | str], _DelegatedCallEntry] = {}


def _delegated_call_key(
    runner: ExtensionRunnerInfo, parent_partial_token: int | str
) -> tuple[int, int | str]:
    return (id(runner.client), parent_partial_token)


async def _discard_delegated_output(
    ctx: proxy_utils.RunWithPartialResultsContext,
) -> None:
    """Consume both shared outputs to their end, discarding.

    Final-sink iteration still acknowledges every fence, so the child run's
    drain completes with its own outcome; nothing is forwarded to the parent.
    """
    async for _ in ctx:
        pass
    if ctx.progress is not None:
        async for _ in ctx.progress:
            pass


def _nearest_projects_hint(path: Path, ws_context: context.WorkspaceContext) -> str:
    """Name the workspace projects lying closest to *path* on disk.

    A workspace can hold a hundred projects, and listing all of them buries the
    answer. The paths that get asked for and turn out not to be projects are
    almost never random — they are a real project's path with a segment too many
    or too few — so the projects sharing the longest prefix with the bad path are
    the ones worth showing.
    """
    known = list(ws_context.ws_projects)
    if not known:
        return "This workspace has no projects."

    def shared_segments(candidate: Path) -> int:
        return sum(
            1 for a, b in zip(path.parts, candidate.parts, strict=False) if a == b
        )

    nearest = sorted(known, key=shared_segments, reverse=True)[
        :_NEAREST_PROJECTS_IN_HINT
    ]
    rest = len(known) - len(nearest)
    suffix = f" (and {rest} more)" if rest > 0 else ""
    return f"Nearest projects: {', '.join(str(p) for p in nearest)}{suffix}."


async def _workspace_root_hosting(
    action_name: str, ws_context: context.WorkspaceContext
) -> Path:
    """The workspace root, which hosts a workspace-scoped action dispatched
    without project paths.

    Fails rather than falling back to every declaring project: that fallback is
    the N-instances-each-fanning-out-to-N shape this routing exists to prevent.
    """
    workspace_root = context.pick_workspace_root_dir(ws_context)
    if workspace_root is None:
        raise errors.ProjectError(
            f"Cannot run workspace-scoped '{action_name}': the workspace root is "
            "ambiguous (several workspace directories, none with "
            "finecode-workspace.toml). Pass project paths explicitly."
        )
    try:
        root_project = (
            await project_resolution_service.ensure_projects_resolved(
                [workspace_root], ws_context
            )
        ).require([workspace_root])[0]
    except project_resolution_service.ProjectResolutionFailed as exc:
        raise errors.ProjectError(
            f"Cannot run workspace-scoped '{action_name}' in the workspace root: "
            f"{exc.message}"
        ) from exc
    if not any(a.name == action_name for a in root_project.actions):
        raise errors.ActionNotFoundError(
            f"Workspace-scoped action '{action_name}' is not declared in the "
            f"workspace root project {workspace_root}, which hosts it when no "
            "project paths are given"
        )
    return workspace_root


def _origin_of_calling_run(
    run_id: str | None,
) -> elicitation_bridge.RunDispatchOrigin:
    """The origin to dispatch this call under: whoever started the run that
    asked for it.

    A run that streams belongs to the client that started it. When a handler in
    that run dispatches another action, the run the WM mints for it is a
    continuation of the same work and belongs to the same person — so a question
    asked from inside it must reach them, not nobody (ADR-0082 rule 1).

    Inheritance is by run id rather than by project because that is the only
    thing that identifies *which* run is calling: the runner making the call may
    serve a project two clients are both using. Depth beyond one hop needs no
    special handling — the nested run is bound to the same connection, so its
    own dispatches inherit it in turn.
    """
    return elicitation_bridge.RunDispatchOrigin(
        connection=elicitation_bridge.originating_client_for_run(run_id)
    )


class _BridgeHandlers:
    """``run_dispatch_bridge``'s slot, filled by the module that owns run dispatch."""

    async def run_action_in_project(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInProjectParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.RunActionInProjectResult:
        executor = ProjectExecutor(ws_context)

        # The nested run gets its own id, so it needs its own binding: inheriting
        # the caller's connection is what lets a question asked inside it reach
        # the same person. Same-project dispatch is no exception — the run doing
        # the asking is a different one from the run that was addressed.
        origin = _origin_of_calling_run(params.run_id)
        return await self._run_action_in_project(runner, params, executor, origin)

    async def _run_action_in_project(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInProjectParams,
        executor: ProjectExecutor,
        origin: elicitation_bridge.RunDispatchOrigin,
    ) -> _internal_client_types.RunActionInProjectResult:
        if params.partial_result_token is not None:
            # register synchronously, before this handler's first
            # suspension point, so a release read after this request finds it.
            parent_partial_token = params.partial_result_token
            call_key = _delegated_call_key(runner, parent_partial_token)
            call_entry = _DelegatedCallEntry(state="registered")
            _delegated_calls[call_key] = call_entry
            try:
                return await self._run_streaming_action(
                    runner, params, executor, origin, call_entry
                )
            finally:
                _ = _delegated_calls.pop(call_key, None)

        result = await executor.run_action(
            action_source=params.action_source,
            params=params.payload,
            project_path=runner.working_dir_path,
            run_trigger=RunActionTrigger(params.meta.trigger),
            dev_env=DevEnv(params.meta.dev_env),
            orchestration_depth=params.meta.orchestration_depth,
            caller_kwargs=params.caller_kwargs,
            origin=origin,
        )
        return _internal_client_types.RunActionInProjectResult(
            result=result.result_by_format.get("json", {}),
            return_code=result.return_code,
        )

    async def _run_streaming_action(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInProjectParams,
        executor: ProjectExecutor,
        origin: elicitation_bridge.RunDispatchOrigin,
        call_entry: _DelegatedCallEntry,
    ) -> _internal_client_types.RunActionInProjectResult:
        parent_partial_token = params.partial_result_token
        assert parent_partial_token is not None
        child_partial_token = str(uuid.uuid4())
        partial_count = 0
        child_progress_token: str | None = None
        if params.progress_token is not None:
            child_progress_token = str(uuid.uuid4())
        async with executor.run_action_with_partial_results(
            action_source=params.action_source,
            params=params.payload,
            project_path=runner.working_dir_path,
            partial_result_token=child_partial_token,
            progress_token=child_progress_token,
            run_trigger=RunActionTrigger(params.meta.trigger),
            dev_env=DevEnv(params.meta.dev_env),
            orchestration_depth=params.meta.orchestration_depth,
            caller_kwargs=params.caller_kwargs,
            origin=origin,
            delegated_progress_limit=(
                DELEGATED_PROGRESS_PENDING_LIMIT
                if params.progress_token is not None
                else None
            ),
        ) as ctx:
            if (
                params.progress_token is not None
                and ctx.progress is not None
                and call_entry.state != "released"
            ):
                progress_token = params.progress_token

                async def _pump_partials() -> None:
                    nonlocal partial_count
                    async for partial_raw in ctx:
                        if call_entry.state == "released":
                            # Released: keep consuming so fences complete,
                            # but send nothing further to the parent.
                            continue
                        partial_count += 1
                        assert runner.client is not None
                        await runner.client.notify_and_drain(
                            _internal_client_types.PROGRESS,
                            _internal_client_types.ProgressParams(
                                token=parent_partial_token,
                                value=json.dumps(partial_raw.get("json", {})),
                            ),
                        )

                async def _pump_progress() -> None:
                    assert ctx.progress is not None
                    forwarding = True
                    async for progress_raw in ctx.progress:
                        if call_entry.state == "released" or not forwarding:
                            # A release, like a send failure, stops progress
                            # forwarding for the call, but the stream is still
                            # consumed (discarded) so its fences complete and
                            # the child run's drain cannot stall.
                            continue
                        assert runner.client is not None
                        try:
                            await runner.client.notify_and_drain(
                                _internal_client_types.PROGRESS,
                                _internal_client_types.ProgressParams(
                                    token=progress_token,
                                    value=json.dumps(progress_raw),
                                ),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "Delegated progress send failed; stopping"
                                f" progress forwarding: {exc}"
                            )
                            forwarding = False

                partial_pump = asyncio.ensure_future(_pump_partials())
                progress_pump = asyncio.ensure_future(_pump_progress())
                call_entry.state = "forwarding"
                partial_error: BaseException | None = None
                try:
                    try:
                        await partial_pump
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        # A partial-send failure still fails the run
                        # (ADR-0135 rule 3), but it must not cancel the
                        # progress pump: that pump keeps draining the
                        # shared progress stream so its fences complete.
                        partial_error = exc
                    await progress_pump
                finally:
                    for pump in (partial_pump, progress_pump):
                        if not pump.done():
                            _ = pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.gather(
                            partial_pump, progress_pump, return_exceptions=True
                        )
                if call_entry.state != "released" and partial_error is not None:
                    raise partial_error
            elif call_entry.state == "released":
                # Released before dispatch: dispatched as always, but
                # never given forwarding pumps or a parent-facing buffer.
                await _discard_delegated_output(ctx)
            else:
                async for partial_raw in ctx:
                    if call_entry.state == "released":
                        # Released mid-stream: keep consuming so fences
                        # complete, but send nothing further to the parent.
                        continue
                    partial_count += 1
                    assert runner.client is not None
                    await runner.client.notify_and_drain(
                        _internal_client_types.PROGRESS,
                        _internal_client_types.ProgressParams(
                            token=parent_partial_token,
                            value=json.dumps(partial_raw.get("json", {})),
                        ),
                    )
            if ctx.responses:
                final = ctx.responses[0]
                if call_entry.state != "released" and final.status != "streamed":
                    final_json = final.result_by_format.get("json", {})
                    if partial_count == 0 and final_json:
                        assert runner.client is not None
                        await runner.client.notify_and_drain(
                            _internal_client_types.PROGRESS,
                            _internal_client_types.ProgressParams(
                                token=parent_partial_token,
                                value=json.dumps(final_json),
                            ),
                        )
                return _internal_client_types.RunActionInProjectResult(
                    return_code=final.return_code,
                )
            return _internal_client_types.RunActionInProjectResult(
                return_code=0,
            )

    async def release_delegated_call(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.ReleaseDelegatedCallParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.ReleaseDelegatedCallResult:
        """Release a delegated call the parent closed early."""
        _ = ws_context
        entry = _delegated_calls.pop(
            _delegated_call_key(runner, params.partial_result_token), None
        )
        if entry is None:
            return _internal_client_types.ReleaseDelegatedCallResult()
        # Flag-only: the forwarding pumps observe ``released`` and turn into
        # discarding drains. They are never cancelled here: cancelling a pump
        # suspended mid-handoff would strand a consumed-but-unacknowledged
        # fence and stall the child run's drain, so the child would never
        # complete. Replying needs no pump rendezvous.
        entry.state = "released"
        return _internal_client_types.ReleaseDelegatedCallResult()

    async def run_action_in_workspace(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.RunActionInWorkspaceParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.RunActionInWorkspaceResult:
        run_trigger = RunActionTrigger(params.meta.trigger)
        dev_env = DevEnv(params.meta.dev_env)

        # Resolve action name from source via the runner's own project actions.
        # Use canonical_source (resolved by ER)
        project = ws_context.ws_projects.get(runner.working_dir_path)
        if not isinstance(project, domain.CollectedProject):
            raise errors.InternalError(
                f"Project {runner.working_dir_path} has no valid config"
            )

        action = await find_action_by_canonical_source(
            params.action_source, project, ws_context
        )
        action_name = action.name if action is not None else None

        if action_name is None:
            known = [
                f"{a.name}(source={a.source!r}, canonical={a.canonical_source!r})"
                for a in project.actions
            ]
            logger.info(
                f"run_action_in_workspace: action_source={params.action_source!r} not found"
                f" in project {runner.working_dir_path}."
                f" Known actions ({len(known)}): {known}"
            )
            raise errors.ActionNotFoundError(
                f"No action with source '{params.action_source}' found in project {runner.working_dir_path}"
            )

        if params.project_paths:
            # The paths come from a handler's payload, which the WM has never
            # vetted — typically a caller-supplied URI that some ER turned into
            # a path. An unknown one used to surface as a bare KeyError from the
            # dict lookup deep in proxy_utils, whose message was the repr of a
            # PosixPath and nothing else.
            requested = [Path(p) for p in params.project_paths]
            unknown = [p for p in requested if p not in ws_context.ws_projects]
            if unknown:
                raise errors.ProjectError(
                    f"Cannot run '{action_name}': "
                    f"{', '.join(str(p) for p in unknown)} "
                    f"{'are' if len(unknown) > 1 else 'is'} not a project in this "
                    f"workspace (requested by {runner.working_dir_path}). "
                    + " ".join(_nearest_projects_hint(p, ws_context) for p in unknown)
                )
            actions_by_project = {p: [action_name] for p in requested}
        elif action is not None and action.scope == domain.ActionScope.WORKSPACE:
            # Matching on canonical_source above means the metadata, and so the
            # scope, is resolved. Mirrors the client API route (_helpers.py).
            root = await _workspace_root_hosting(action_name, ws_context)
            actions_by_project = {root: [action_name]}
        else:
            actions_by_project = {
                p: [action_name]
                for p in await find_all_projects_with_action(action_name, ws_context)
            }

        executor = WorkspaceExecutor(ws_context)
        # Override keys arrive as the ER serialized them (`as_posix()`). The
        # `proxy_utils` lookup uses `str(Path)`, and the two forms differ on
        # Windows (`C:\ws\proj` vs `C:/ws/proj`) — normalize here at the one
        # new boundary so no project silently receives the empty base payload
        # (ADR-0090, D-B1).
        payload_overrides_by_project = {
            str(Path(k)): v
            for k, v in (params.payload_overrides_by_project or {}).items()
        } or None
        results = await executor.run_actions_in_projects(
            actions_by_project=actions_by_project,
            params=params.payload,
            run_trigger=run_trigger,
            dev_env=dev_env,
            orchestration_depth=params.meta.orchestration_depth,
            concurrently=params.concurrently,
            payload_overrides_by_project=payload_overrides_by_project,
            origin=_origin_of_calling_run(params.run_id),
        )
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

    async def get_actions_for_parent(
        self,
        runner: ExtensionRunnerInfo,
        params: _internal_client_types.GetActionsForParentParams,
        ws_context: context.WorkspaceContext,
    ) -> _internal_client_types.GetActionsForParentResult:
        """Serve ``finecode/getActionsForParent`` (ADR-0045).

        Lists every action in this project that specializes the given parent
        action, regardless of which env owns its handler — an ER only ever
        knows the actions its own env executes, so this cross-env picture can
        only come from the WM. Resolution (including on-demand env startup
        for actions not yet importable by any runner) is delegated to
        ``find_subactions_for_parent``/``ensure_action_metadata``, the same
        machinery used elsewhere to resolve action metadata.
        """
        project = ws_context.ws_projects.get(runner.working_dir_path)
        if not isinstance(project, domain.CollectedProject):
            raise errors.ConfigurationError(
                f"Project '{runner.working_dir_path}' has no valid config"
            )

        subactions = await find_subactions_for_parent(
            params.parent_action_source, project, ws_context
        )
        return _internal_client_types.GetActionsForParentResult(
            subactions=[
                _internal_client_types.SubactionInfo(
                    source=a.source,
                    canonical_source=a.canonical_source,
                    language=a.language,
                )
                for a in subactions
            ]
        )

    async def list_workspace_actions(
        self, ws_context: context.WorkspaceContext
    ) -> dict:
        """Serve ``finecode/listWorkspaceActions`` (ER → WM).

        Returns the aggregated action/handler registry across every project and
        env in the workspace.
        """
        outcome = await project_resolution_service.ensure_all_projects_resolved(
            ws_context
        )
        if outcome.failed:
            reasons = "; ".join(
                f"{p}: {reason}" for p, reason in outcome.failed.items()
            )
            raise errors.ProjectError(f"Cannot collect workspace actions: {reasons}")
        actions: list[dict] = []
        for project in outcome.resolved.values():
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


run_dispatch_bridge.install(_BridgeHandlers())
