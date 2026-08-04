from __future__ import annotations

import dataclasses

from fine_git.push_git_refs_action import PushGitRefsAction, PushGitRefsRunPayload
from fine_release.release_package_action import (
    PackageReleaseOutcome,
    ReleasePackageAction,
    ReleasePackageRunPayload,
)
from fine_release.release_workspace_packages_action import (
    PackageReleaseResult,
    ReleaseWorkspacePackagesAction,
    ReleaseWorkspacePackagesRunContext,
    ReleaseWorkspacePackagesRunPayload,
    ReleaseWorkspacePackagesRunResult,
    _Candidate,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectinfoprovider,
    iworkspaceactionrunner,
)


@dataclasses.dataclass
class SweepReleasePackagesHandlerConfig(code_action.ActionHandlerConfig): ...


class SweepReleasePackagesHandler(
    code_action.ActionHandler[
        ReleaseWorkspacePackagesAction,
        SweepReleasePackagesHandlerConfig,
    ]
):
    """Release each candidate in dependency order, then publish the refs the
    run produced in a single push.

    Every per-package step is delegated to the package's own `release_package`
    chain (ADR-0065); what stays here is ordering, blocking, and the one
    repository-wide operation.
    """

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        project_info: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.project_info = project_info
        self.logger = logger

    async def run(
        self,
        payload: ReleaseWorkspacePackagesRunPayload,
        run_context: ReleaseWorkspacePackagesRunContext,
    ) -> ReleaseWorkspacePackagesRunResult:
        state = run_context.state
        meta = run_context.meta

        packages: list[PackageReleaseResult] = []
        outcomes_by_name: dict[str, PackageReleaseOutcome] = {}
        created_refs: list[str] = []

        for name in state.ordered_names:
            candidate = state.candidates_by_name[name]

            blocking_deps = {
                dep
                for dep in state.dependencies_by_name.get(name, set())
                if outcomes_by_name.get(dep)
                in (PackageReleaseOutcome.FAILED, PackageReleaseOutcome.BLOCKED)
            }
            if blocking_deps and not payload.dry_run:
                package = PackageReleaseResult(
                    package_name=candidate.package_name,
                    src_artifact_def_path=candidate.src_artifact_def_path,
                    version=candidate.version,
                    outcome=PackageReleaseOutcome.BLOCKED,
                    registries=[],
                )
            else:
                package, package_refs = await self._release_one(
                    candidate, payload.dry_run, meta
                )
                created_refs.extend(package_refs)

            outcomes_by_name[name] = package.outcome
            packages.append(package)

        # Unconditional: a package that failed must not discard the records of
        # every package that succeeded (ADR-0065).
        push_error = await self._push_refs(created_refs, meta)

        return ReleaseWorkspacePackagesRunResult(
            dry_run=payload.dry_run,
            packages=packages,
            error=push_error,
        )

    async def _release_one(
        self,
        candidate: _Candidate,
        dry_run: bool,
        meta: code_action.RunActionMeta,
    ) -> tuple[PackageReleaseResult, list[str]]:
        try:
            results = await self.workspace_action_runner.run_action_in_projects(
                action_type=ReleasePackageAction,
                payload=ReleasePackageRunPayload(
                    package_name=candidate.package_name,
                    version=candidate.version,
                    src_artifact_def_path=candidate.src_artifact_def_path,
                    dry_run=dry_run,
                ),
                meta=meta,
                project_paths=[candidate.project_path],
                concurrently=True,
            )
        except Exception as exception:
            # Last resort only: build and publish failures come back as a FAILED
            # result carrying their own message. Reaching here means the release
            # could not be run at all — a runner crash or transport error — and
            # must fail this package without stopping the sweep.
            self.logger.error(
                f"Could not run release_package for {candidate.package_name}: {exception}"
            )
            return (
                PackageReleaseResult(
                    package_name=candidate.package_name,
                    src_artifact_def_path=candidate.src_artifact_def_path,
                    version=candidate.version,
                    outcome=PackageReleaseOutcome.FAILED,
                    registries=[],
                    error=str(exception),
                ),
                [],
            )

        release_result = results[candidate.project_path]

        return (
            PackageReleaseResult(
                package_name=candidate.package_name,
                src_artifact_def_path=candidate.src_artifact_def_path,
                version=candidate.version,
                outcome=release_result.outcome,
                registries=release_result.registries,
                error=release_result.error,
            ),
            list(release_result.created_refs),
        )

    async def _push_refs(
        self, refs: list[str], meta: code_action.RunActionMeta
    ) -> str | None:
        """Push the refs the run produced. Returns an error message when the push
        did not reach the remote, so the sweep can fail the run rather than let a
        lost push hide behind a green result (ADR-0060). None on success or when
        there is nothing to push."""
        if not refs:
            return None

        # A push acts on the one repository shared by every package, so it is
        # targeted at the workspace root — which is where this handler runs —
        # rather than fanned out per project.
        workspace_root = self.project_info.get_current_project_dir_path()

        try:
            push_results = await self.workspace_action_runner.run_action_in_projects(
                action_type=PushGitRefsAction,
                payload=PushGitRefsRunPayload(refs=refs),
                meta=meta,
                project_paths=[workspace_root],
                concurrently=True,
            )
        except Exception as exception:
            error = f"Failed to push release tags {refs}: {exception}"
            self.logger.warning(error)
            return error

        push_result = push_results[workspace_root]
        if push_result.error is not None:
            error = f"Failed to push release tags {refs}: {push_result.error}"
            self.logger.warning(error)
            return error

        return None
