"""Fills ``runner.run_dispatch_bridge``'s slot for ER-initiated action dispatch.

Imported for its ``install`` side effect by ``run_service/__init__.py``, the same way
``knowledge_service.py`` fills ``runner.knowledge_bridge``. See ADR-0072 for why the
runner reaches this code through a slot rather than importing it.
"""

from __future__ import annotations

import asyncio
import json
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
from finecode.wm_server.services.run_service.project_executor import ProjectExecutor
from finecode.wm_server.services.run_service.proxy_utils import (
    ensure_action_metadata,
    find_all_projects_with_action,
    find_subactions_for_parent,
)
from finecode.wm_server.services.run_service.workspace_executor import WorkspaceExecutor

_NEAREST_PROJECTS_IN_HINT = 3


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
        params: "_internal_client_types.RunActionInProjectParams",
        ws_context: context.WorkspaceContext,
    ) -> "_internal_client_types.RunActionInProjectResult":
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
        # Back-channel project dispatches never wait for the budget. A streaming
        # child arrives at depth 0 (the streaming path forwards no
        # orchestrationDepth), so if the run that asked for it holds slots it
        # would deadlock waiting on them; declaring waits=False is what gives it
        # the one slot it needs (ADR-0094).
        if params.partial_result_token is not None:
            partial_count = 0
            async with executor.run_action_with_partial_results(
                action_source=params.action_source,
                params=params.payload,
                project_path=runner.working_dir_path,
                partial_result_token=params.partial_result_token,
                run_trigger=RunActionTrigger(params.meta.trigger),
                dev_env=DevEnv(params.meta.dev_env),
                orchestration_depth=params.meta.orchestration_depth,
                caller_kwargs=params.caller_kwargs,
                budget=domain.RunBudget(waits=False),
                origin=origin,
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

        result = await executor.run_action(
            action_source=params.action_source,
            params=params.payload,
            project_path=runner.working_dir_path,
            run_trigger=RunActionTrigger(params.meta.trigger),
            dev_env=DevEnv(params.meta.dev_env),
            orchestration_depth=params.meta.orchestration_depth,
            caller_kwargs=params.caller_kwargs,
            budget=domain.RunBudget(waits=False),
            origin=origin,
        )
        return _internal_client_types.RunActionInProjectResult(
            result=result.result_by_format.get("json", {}),
            return_code=result.return_code,
        )

    async def run_action_in_workspace(
        self,
        runner: ExtensionRunnerInfo,
        params: "_internal_client_types.RunActionInWorkspaceParams",
        ws_context: context.WorkspaceContext,
    ) -> "_internal_client_types.RunActionInWorkspaceResult":
        run_trigger = RunActionTrigger(params.meta.trigger)
        dev_env = DevEnv(params.meta.dev_env)

        # Resolve action name from source via the runner's own project actions.
        # Use canonical_source (resolved by ER)
        project = ws_context.ws_projects.get(runner.working_dir_path)
        if not isinstance(project, domain.CollectedProject):
            raise errors.InternalError(
                f"Project {runner.working_dir_path} has no valid config"
            )

        def _find_action_name() -> str | None:
            return next(
                (
                    a.name
                    for a in project.actions
                    if a.canonical_source == params.action_source
                ),
                None,
            )

        action_name = _find_action_name()
        if action_name is None:
            # canonical_source is resolved asynchronously by each env's runner
            # (update_runner_config -> resolveActionMeta). Right after a restart
            # the runner that owns this action's handlers may still be
            # initializing when this back-channel call arrives. Give any
            # not-yet-resolved action in this project a chance to resolve
            # before giving up, reusing the same mechanism the external API
            # boundary already relies on (ensure_action_metadata). Each attempt
            # is independent — one action's metadata being unresolvable must
            # not cancel another action's resolution that is about to succeed,
            # so gather (not TaskGroup) with return_exceptions=True.
            # TODO: untested. Needs a unit test with ensure_action_metadata
            # stubbed to resolve canonical_source as a side effect (race
            # recovers) and stubbed as a no-op (still raises
            # ActionNotFoundError).
            unresolved = [a for a in project.actions if a.canonical_source is None]
            if unresolved:
                await asyncio.gather(
                    *(
                        ensure_action_metadata(a, project, ws_context)
                        for a in unresolved
                    ),
                    return_exceptions=True,
                )
                action_name = _find_action_name()

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
        else:
            actions_by_project = {
                p: [action_name]
                for p in find_all_projects_with_action(action_name, ws_context)
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
        params: "_internal_client_types.GetActionsForParentParams",
        ws_context: context.WorkspaceContext,
    ) -> "_internal_client_types.GetActionsForParentResult":
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


run_dispatch_bridge.install(_BridgeHandlers())
