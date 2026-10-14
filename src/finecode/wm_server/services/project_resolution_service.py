"""On-demand project resolution for WM entry points (ADR-0101).

A project's action set is only complete after its presets resolve, and preset
resolution asks that project's own running ``dev_workspace`` ER where preset
packages live.  ``ensure_projects_resolved`` / ``ensure_all_projects_resolved``
are the gates every entry point that reads a project's actions or
preset-dependent config passes through.  They resolve exactly the projects the
caller needs, wait on any resolution already in flight (a project is read only
after its resolution has finished configuring its runners), and remember
attributable failures until the project's configuration is reloaded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import typing
from pathlib import Path

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import action_lookup, runner_start_service

_RETRY_HINT = (
    " — not retried by this workspace server until the project's configuration"
    " is reloaded (python -m finecode reload-config --shared-server --project=<path>)."
    " A new 'finecode run' without --shared-server starts a fresh server."
)


class ProjectResolutionFailed(Exception):
    """One or more projects could not be resolved before they were needed.

    ``message`` names every failed project and its reason; the base class is
    initialized with it so ``str(exc)`` carries it too (a generic handler that
    formats ``str(exc)`` reports the named failure).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclasses.dataclass(frozen=True)
class ResolutionOutcome:
    """The result of resolving a set of projects.

    Attributes:
        resolved: The *fresh* project objects, read from ``ws_context`` after
            resolution.  Never reuse a project reference taken before the gate.
        failed: path → reason, for every requested path that could not be
            resolved.  Enumerating callers report these; explicit callers raise
            through :meth:`require` / :meth:`require_all`.
    """

    resolved: dict[Path, domain.ResolvedProject]
    failed: dict[Path, str]

    def require(self, paths: typing.Iterable[Path]) -> list[domain.ResolvedProject]:
        """The fresh objects for *paths*, in order.

        Raises:
            ProjectResolutionFailed: names every path not in ``resolved``,
                using ``failed`` for its reason when present.
        """
        ordered = list(paths)
        missing = [p for p in ordered if p not in self.resolved]
        if missing:
            reasons = "; ".join(
                f"{p}: {self.failed.get(p, f'{p} is not a resolvable project in this workspace')}"
                for p in missing
            )
            raise ProjectResolutionFailed(reasons)
        return [self.resolved[p] for p in ordered]

    def require_all(self) -> dict[Path, domain.ResolvedProject]:
        """``resolved``, or a failure naming every path in ``failed``."""
        if self.failed:
            reasons = "; ".join(f"{p}: {reason}" for p, reason in self.failed.items())
            raise ProjectResolutionFailed(reasons)
        return self.resolved


@dataclasses.dataclass(frozen=True)
class HostingResolution:
    """Who hosts a root-first resolution of *requested* actions.

    Attributes:
        root_only: True when every requested action is workspace-scoped and in
            the (resolved) workspace root — then only the root was resolved.
        outcome: ``root_only`` → the root alone; otherwise every
            ``CONFIG_VALID`` project.
    """

    root_only: bool
    outcome: ResolutionOutcome


async def resolve_hosting_projects(
    requested: list[str],
    by: typing.Literal["name", "source"],
    ws_context: context.WorkspaceContext,
) -> HostingResolution:
    """Root-first resolution for a run's (or listing's) requested actions.

    Resolves the workspace root first — its resolved config is the only place
    the requested actions' scopes are known — and returns ``HostingResolution``
    with ``root_only`` True when every requested action is workspace-scoped and
    hosted on the root: then nothing else was resolved.  Otherwise every
    ``CONFIG_VALID`` project is resolved (the root is already resolved, so its
    batch holds only the non-root projects), the root's now-resolved metadata
    is propagated onto the fresh sibling objects, and the outcome carries all
    of them.

    Raises:
        ProjectResolutionFailed: when the root itself could not be resolved —
            the root is needed to decide anything (and for a project-scoped
            action it is a target itself); or the caller then fails
            ``require_all`` on the outcome (fan-outs fail loudly instead of
            skipping a failed sibling).  A root action whose metadata cannot
            be resolved propagates that failure rather than falling back to
            full resolution (scope would stay unknown).
    """
    root_path = context.pick_workspace_root_dir(ws_context)
    if root_path is not None and root_path not in ws_context.ws_projects:
        root_path = None
    if root_path is None or ws_context.ws_projects[root_path].status != (
        domain.ProjectStatus.CONFIG_VALID
    ):
        return HostingResolution(False, await ensure_all_projects_resolved(ws_context))

    root = (await ensure_projects_resolved([root_path], ws_context)).require(
        [root_path]
    )[0]

    found: list[domain.Action] = []
    for key in requested:
        if by == "name":
            action = next((a for a in root.actions if a.name == key), None)
        else:
            action = await action_lookup.find_action_by_source(
                root.actions, key, root, ws_context
            )
        if action is None:
            # The root does not say who hosts this action; resolving everything
            # is the only way to find out.
            return HostingResolution(
                False, await ensure_all_projects_resolved(ws_context)
            )
        if action.canonical_source is None:
            # Scope is untrusted until the metadata resolves;
            # a failure propagates — no full-resolution fallback.
            from finecode.wm_server.services.run_service import proxy_utils

            await proxy_utils.ensure_action_metadata(action, root, ws_context)
        found.append(action)

    if all(a.scope == domain.ActionScope.WORKSPACE for a in found):
        return HostingResolution(
            True, ResolutionOutcome(resolved={root_path: root}, failed={})
        )

    outcome = await ensure_all_projects_resolved(ws_context)
    # The batch replaced every sibling object; put the root's class-level
    # metadata onto the fresh objects so the first sibling does not pay a
    # second handler-env start for the same action class.
    fresh_root = ws_context.ws_projects[root_path]
    for action in found:
        if action.canonical_source is not None:
            runner_manager.propagate_action_meta(action, fresh_root, ws_context)
    return HostingResolution(False, outcome)


def _msg(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc) or repr(exc)


def _attribute(
    exc: Exception | None, path: Path, *, batch_size: int
) -> tuple[str, bool]:
    """Whether a batch failure belongs to *path* — and if so, whether it should
    be remembered: an exception naming the project, or a
    single-path batch's exception, is its own; anything else is not."""
    if (
        isinstance(exc, runner_manager.ProjectsFailedToResolve)
        and path in exc.per_project
    ):
        return (_msg(exc.per_project[path]), True)
    if batch_size == 1 and exc is not None:
        return (_msg(exc), True)
    if exc is None:
        return ("resolution finished without resolving the project", False)
    return (f"resolution of a batch including this project failed: {_msg(exc)}", False)


async def ensure_projects_resolved(
    paths: typing.Iterable[Path],
    ws_context: context.WorkspaceContext,
) -> ResolutionOutcome:
    """Resolve the explicitly named *paths*: every one ends up in ``resolved``
    or ``failed``.  An unknown, ``NO_FINECODE`` or ``CONFIG_INVALID`` path goes
    to ``failed`` with that reason — never a silent skip.

    The gate: wait on any resolution already in flight for the
    targets, then classify, then start one batch for everything still
    unresolved.
    """
    targets = list(paths)

    async def _resolve_batch(batch_paths: list[Path]) -> dict[Path, str]:
        """Run one batch, then attribute its outcome per path.

        Invariant: nothing reachable from here calls a gate for a
        path of this batch — ``start_runners_with_auto_prepare``'s in-process
        env installs and the batch projects' own back-channel calls make no
        gated requests (ADR-0101).
        """
        exc: Exception | None = None
        try:
            projects = [
                ws_context.ws_projects[p]
                for p in batch_paths
                if p in ws_context.ws_projects
            ]
            logger.debug(
                f"On-demand project resolution batch: {len(batch_paths)} project(s)"
            )
            await runner_start_service.start_runners_with_auto_prepare(
                projects=projects,
                ws_context=ws_context,
                initialize_all_handlers=False,
            )
        except (
            Exception
        ) as e:  # CancelledError propagates: not caught, nothing recorded
            exc = e
        finally:
            for p in batch_paths:
                if ws_context.project_resolution_tasks.get(p) is asyncio.current_task():
                    del ws_context.project_resolution_tasks[p]

        failures: dict[Path, str] = {}
        for p in batch_paths:
            if p not in ws_context.ws_projects:  # removed meanwhile (rule 9)
                ws_context.ws_projects_raw_configs.pop(p, None)
                continue
            if isinstance(ws_context.ws_projects[p], domain.ResolvedProject):
                continue
            reason, attributable = _attribute(exc, p, batch_size=len(batch_paths))
            failures[p] = reason
            logger.info(f"Project {p} failed to resolve: {reason}")
            if attributable:
                ws_context.project_resolution_failures[p] = reason + _RETRY_HINT
        return failures

    this_call_failures: dict[Path, str] = {}

    in_flight = {
        path: future
        for path, future in ws_context.project_resolution_tasks.items()
        if path in targets
    }
    if in_flight:
        distinct = list(dict.fromkeys(in_flight.values()))
        results = await asyncio.gather(*(asyncio.shield(f) for f in distinct))
        for result in results:
            this_call_failures.update(result)

    classified: dict[Path, str] = {}
    to_resolve: list[Path] = []

    for path in targets:
        project = ws_context.ws_projects.get(path)
        if isinstance(project, domain.ResolvedProject):
            continue
        if project is None:
            classified[path] = f"'{path}' is not a project in this workspace"
            continue
        if project.status != domain.ProjectStatus.CONFIG_VALID:
            classified[path] = f"project '{path}' has status {project.status.name}"
            continue
        if path in this_call_failures:
            # Resolved by someone else's batch during the wait, and it failed:
            # not retried within this call.
            classified[path] = this_call_failures[path]
            continue
        if path in ws_context.project_resolution_failures:
            classified[path] = ws_context.project_resolution_failures[path]
            continue
        to_resolve.append(path)

    if to_resolve:
        batch = asyncio.create_task(_resolve_batch(to_resolve))
        for path in to_resolve:
            ws_context.project_resolution_tasks[path] = batch
        result = await asyncio.shield(batch)
        this_call_failures.update(result)
        for path in to_resolve:
            project = ws_context.ws_projects.get(path)
            if isinstance(project, domain.ResolvedProject):
                continue
            if project is None:
                classified[path] = f"'{path}' is not a project in this workspace"
            elif path in this_call_failures:
                classified[path] = this_call_failures[path]
            else:
                classified[path] = "resolution finished without resolving the project"

    resolved: dict[Path, domain.ResolvedProject] = {
        path: ws_context.ws_projects[path]
        for path in targets
        if isinstance(ws_context.ws_projects.get(path), domain.ResolvedProject)
    }
    return ResolutionOutcome(resolved=resolved, failed=classified)


async def ensure_all_projects_resolved(
    ws_context: context.WorkspaceContext,
) -> ResolutionOutcome:
    """Every ``CONFIG_VALID`` project in the workspace.

    ``NO_FINECODE`` and ``CONFIG_INVALID`` projects are not targets and not in
    ``failed`` — their status is already reported by ``workspace/listProjects``
    and the action tree, and enumerating callers exclude them.
    """
    targets = [
        project.dir_path
        for project in ws_context.ws_projects.values()
        if project.status == domain.ProjectStatus.CONFIG_VALID
    ]
    return await ensure_projects_resolved(targets, ws_context)
