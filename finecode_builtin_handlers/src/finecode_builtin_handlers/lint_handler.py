# docs: docs/reference/actions.md
import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import list_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import lint_action, lint_files_action
from finecode_extension_api.interfaces import (
    ifileeditor,
    ilogger,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import actionable_project_paths
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri, resource_uri_to_path
from finecode_extension_api.workspace_utils import group_files_by_project


@dataclasses.dataclass
class LintHandlerConfig(code_action.ActionHandlerConfig):
    lint_opened_files_only_in_ide: bool = True
    """When True (default), background IDE lints triggered automatically only lint
    currently opened files for performance. Set to False to always lint the full workspace."""


async def _list_workspace_files(
    project_paths: list[pathlib.Path],
    workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    meta: code_action.RunActionMeta,
) -> list[pathlib.Path]:
    """List all source files across the given projects by calling ListSrcArtifactFilesByLangAction."""
    results = await workspace_action_runner.run_action_in_projects(
        action_type=list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangAction,
        payload=list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload(
            langs=None
        ),
        meta=meta,
        project_paths=project_paths,
        concurrently=True,
    )
    files: list[pathlib.Path] = []
    for result in results.values():
        for file_list in result.files_by_lang.values():
            files.extend(resource_uri_to_path(f) for f in file_list)
    return files


class LintHandler(
    code_action.ActionHandler[
        lint_action.LintAction, LintHandlerConfig
    ]
):
    def __init__(
        self,
        config: LintHandlerConfig,
        workspace_action_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.workspace_action_runner = workspace_action_runner
        self.workspace_info_provider = workspace_info_provider
        self.file_editor = file_editor
        self.logger = logger

    async def _lint_project(
        self,
        project_path: pathlib.Path,
        project_files: list[pathlib.Path],
        run_meta: code_action.RunActionMeta,
        progress,
        partial_result_sender,
    ) -> None:
        project_file_uris = [path_to_resource_uri(f) for f in project_files]
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=lint_files_action.LintFilesAction,
            payload=lint_files_action.LintFilesRunPayload(file_paths=project_file_uris),
            meta=run_meta,
            project_paths=[project_path],
        )
        for result in results.values():
            uris = list(result.messages)
            msg = str(uris[0]) if uris else None
            if len(uris) > 1:
                msg += f" and {len(uris) - 1} related"
            await progress.advance(steps=len(project_files), message=msg)
            await partial_result_sender.send(lint_action.LintRunResult(messages=result.messages))

    async def run(
        self,
        payload: lint_action.LintRunPayload,
        run_context: lint_action.LintRunContext,
    ):
        run_meta = run_context.meta

        project_paths = (
            [resource_uri_to_path(uri) for uri in payload.project_paths]
            if payload.project_paths is not None
            else actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        )

        file_uris: list[ResourceUri]
        if payload.target == lint_action.LintTarget.FILES:
            file_uris = payload.file_paths
        elif (
            self.config.lint_opened_files_only_in_ide
            and run_meta.dev_env == code_action.DevEnv.IDE
            and run_meta.trigger == code_action.RunActionTrigger.SYSTEM
        ):
            file_uris = [
                path_to_resource_uri(p)
                for p in self.file_editor.get_opened_files()
            ]
        else:
            files = await _list_workspace_files(
                project_paths, self.workspace_action_runner, run_meta
            )
            file_uris = [path_to_resource_uri(f) for f in files]

        files_by_project = group_files_by_project(
            [resource_uri_to_path(u) for u in file_uris],
            project_paths,
        )

        async with run_context.progress("Linting files", total=len(file_uris)) as progress:
            async with asyncio.TaskGroup() as tg:
                for project_path, project_files in files_by_project.items():
                    tg.create_task(
                        self._lint_project(
                            project_path,
                            project_files,
                            run_meta,
                            progress,
                            run_context.partial_result_sender,
                        )
                    )
