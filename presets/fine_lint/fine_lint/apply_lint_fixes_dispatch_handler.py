from __future__ import annotations

import asyncio
import dataclasses
import pathlib

from fine_src_artifacts import list_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iuser_messenger,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_api.workspace_utils import group_files_by_project

from fine_lint.apply_lint_fixes_action import (
    ApplyLintFixesAction,
    ApplyLintFixesRunContext,
    ApplyLintFixesRunPayload,
    ApplyLintFixesRunResult,
)
from fine_lint.apply_lint_fixes_files_action import (
    ApplyLintFixesFilesAction,
    ApplyLintFixesFilesRunPayload,
)
from fine_lint.lint_action import LintTarget


@dataclasses.dataclass
class ApplyLintFixesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class ApplyLintFixesDispatchHandler(
    code_action.ActionHandler[ApplyLintFixesAction, ApplyLintFixesDispatchHandlerConfig]
):
    """Bridge handler that routes files to owning projects and runs the
    ``apply_lint_fixes_files`` pass loop once per project.

    Per R-109, this workspace handler never reads project files itself: file
    enumeration for ``target=PROJECT`` goes through the project-scoped
    ``list_src_artifact_files_by_lang`` action, and fixing goes through the
    project-scoped ``apply_lint_fixes_files`` action, both via
    ``IWorkspaceActionRunner``.
    """

    def __init__(
        self,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
        user_messenger: iuser_messenger.IUserMessenger,
    ) -> None:
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger
        self.user_messenger = user_messenger

    async def _project_files(
        self,
        project_paths: list[pathlib.Path],
        run_meta: code_action.RunActionMeta,
    ) -> dict[pathlib.Path, list[ResourceUri]]:
        """Enumerate every source file per project via the project-scoped
        ``list_src_artifact_files_by_lang`` action -- used for
        ``target=PROJECT``, where the caller named no specific files."""
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangAction,
            payload=list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload(
                langs=None
            ),
            meta=run_meta,
            project_paths=project_paths,
        )
        files_by_project: dict[pathlib.Path, list[ResourceUri]] = {}
        for project_path, result in results.items():
            files_by_project[project_path] = [
                uri for files in result.files_by_lang.values() for uri in files
            ]
        return files_by_project

    async def _run_for_project(
        self,
        project_path: pathlib.Path,
        file_uris: list[ResourceUri],
        payload: ApplyLintFixesRunPayload,
        run_meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=ApplyLintFixesFilesAction,
            payload=ApplyLintFixesFilesRunPayload(
                file_paths=file_uris,
                kinds=payload.kinds,
                include_unsafe=payload.include_unsafe,
                max_passes=payload.max_passes,
                dry_run=payload.dry_run,
            ),
            meta=run_meta,
            project_paths=[project_path],
        )
        project_uri = path_to_resource_uri(project_path)
        for result in results.values():
            await partial_result_sender.send(
                ApplyLintFixesRunResult(
                    applied_counts=result.applied_counts,
                    remaining_fixes=result.remaining_fixes,
                    statuses={project_uri: result.status},
                    passes={project_uri: result.passes},
                )
            )

    async def run(
        self,
        payload: ApplyLintFixesRunPayload,
        run_context: ApplyLintFixesRunContext,
    ) -> None:
        if payload.project_paths is not None:
            project_paths = [resource_uri_to_path(uri) for uri in payload.project_paths]
        else:
            project_paths = actionable_project_paths(
                await self.workspace_info_provider.get_workspace_projects()
            )

        tasks: list[tuple[pathlib.Path, list[ResourceUri]]]
        if payload.target == LintTarget.FILES:
            if not payload.file_paths:
                # R-309: nothing was requested -- return without creating any
                # tasks, rather than propagating an empty request downstream.
                return
            file_abs_paths = [resource_uri_to_path(uri) for uri in payload.file_paths]
            project_to_files = group_files_by_project(file_abs_paths, project_paths)
            tasks = [
                (project_path, [path_to_resource_uri(f) for f in files])
                for project_path, files in project_to_files.items()
            ]
            if not tasks:
                message = (
                    "ApplyLintFixesDispatchHandler: none of the requested files "
                    "matched a known project -- no fixes will be applied. "
                    f"file_paths={payload.file_paths}"
                )
                if run_context.meta.trigger == code_action.RunActionTrigger.USER:
                    self.user_messenger.warning(message)
                else:
                    # System-triggered calls routinely include files outside any
                    # known project -- expected, not diagnosable (R-505).
                    self.logger.debug(message)
                return

            # R-307: every requested file must be covered by at least one
            # partial result, including files no project claimed.
            assigned_paths = {f for files in project_to_files.values() for f in files}
            unmatched_uris = [
                uri
                for uri, path in zip(payload.file_paths, file_abs_paths, strict=True)
                if path not in assigned_paths
            ]
            if unmatched_uris:
                await run_context.partial_result_sender.send(
                    ApplyLintFixesRunResult(
                        applied_counts=dict.fromkeys(unmatched_uris, 0)
                    )
                )
        else:
            files_by_project = await self._project_files(
                project_paths, run_context.meta
            )
            tasks = [
                (project_path, file_uris)
                for project_path, file_uris in files_by_project.items()
                if file_uris
            ]
            if not tasks and payload.project_paths is not None:
                # R-505: the caller named specific projects and none of them
                # produced anything to fix.
                message = (
                    "ApplyLintFixesDispatchHandler: none of the requested projects "
                    "could be found or have files to fix -- no fixes will be "
                    f"applied. project_paths={payload.project_paths}"
                )
                if run_context.meta.trigger == code_action.RunActionTrigger.USER:
                    self.user_messenger.warning(message)
                else:
                    self.logger.debug(message)
                return

        async with asyncio.TaskGroup() as tg:
            for project_path, file_uris in tasks:
                tg.create_task(
                    self._run_for_project(
                        project_path,
                        file_uris,
                        payload,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
