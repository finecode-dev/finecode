from __future__ import annotations

import dataclasses
import pathlib

from fine_dep_graph.collect_project_dependency_info_action import (
    CollectProjectDependencyInfoAction,
    CollectProjectDependencyInfoRunPayload,
)
from fine_release.release_workspace_packages_action import (
    ReleaseWorkspacePackagesAction,
    ReleaseWorkspacePackagesRunContext,
    ReleaseWorkspacePackagesRunPayload,
    ReleaseWorkspacePackagesRunResult,
    _Candidate,
)
from fine_src_artifacts.get_src_artifact_version_action import (
    GetSrcArtifactVersionAction,
    GetSrcArtifactVersionRunPayload,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iuser_messenger,
    iworkspaceactionrunner,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri


@dataclasses.dataclass
class DiscoverReleaseCandidatesHandlerConfig(code_action.ActionHandlerConfig): ...


def _src_artifact_def_path(project_path: pathlib.Path) -> ResourceUri:
    return path_to_resource_uri((project_path / "pyproject.toml").resolve())


class DiscoverReleaseCandidatesHandler(
    code_action.ActionHandler[
        ReleaseWorkspacePackagesAction,
        DiscoverReleaseCandidatesHandlerConfig,
    ]
):
    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        logger: ilogger.ILogger,
        user_messenger: iuser_messenger.IUserMessenger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.logger = logger
        self.user_messenger = user_messenger

    async def run(
        self,
        payload: ReleaseWorkspacePackagesRunPayload,
        run_context: ReleaseWorkspacePackagesRunContext,
    ) -> ReleaseWorkspacePackagesRunResult:
        project_paths = (
            [pathlib.Path(p) for p in payload.project_paths]
            if payload.project_paths is not None
            else None
        )

        # src_artifact_def_path=None means "the receiving project's own
        # pyproject.toml" (same convention as BuildArtifactRunPayload) — this
        # payload is broadcast unchanged to every fanned-out project by
        # run_action_in_projects, so it cannot carry a single concrete path.
        version_results = await self.workspace_action_runner.run_action_in_projects(
            action_type=GetSrcArtifactVersionAction,
            payload=GetSrcArtifactVersionRunPayload(src_artifact_def_path=None),
            meta=run_context.meta,
            project_paths=project_paths,
            concurrently=True,
        )

        candidate_paths = list(version_results.keys())
        dependency_info_results = (
            await self.workspace_action_runner.run_action_in_projects(
                action_type=CollectProjectDependencyInfoAction,
                payload=CollectProjectDependencyInfoRunPayload(),
                meta=run_context.meta,
                project_paths=candidate_paths,
                concurrently=True,
            )
        )

        candidates_by_name: dict[str, _Candidate] = {}
        for project_path in candidate_paths:
            package_name = dependency_info_results[project_path].package_name
            candidates_by_name[package_name] = _Candidate(
                project_path=project_path,
                package_name=package_name,
                version=version_results[project_path].version,
                src_artifact_def_path=_src_artifact_def_path(project_path),
            )

        if not candidates_by_name and payload.project_paths is not None:
            message = (
                "DiscoverReleaseCandidatesHandler: none of the requested "
                f"project_paths matched a releasable candidate — no packages will "
                f"be released. project_paths={payload.project_paths}"
            )
            if run_context.meta.trigger == code_action.RunActionTrigger.USER:
                self.user_messenger.warning(message)
            else:
                self.logger.debug(message)

        run_context.state.candidates_by_name = dict(sorted(candidates_by_name.items()))

        return ReleaseWorkspacePackagesRunResult(dry_run=payload.dry_run)
