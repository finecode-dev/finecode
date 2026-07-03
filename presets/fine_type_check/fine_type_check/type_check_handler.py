# docs: docs/reference/actions.md
import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from fine_src_artifacts import list_src_artifact_files_by_lang_action
from fine_inspect_code.diagnostic_types import DiagnosticFilesRunPayload
from fine_type_check import type_check_action
from fine_type_check.type_check_files_action import TypeCheckFilesAction
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
class TypeCheckHandlerConfig(code_action.ActionHandlerConfig):
    type_check_opened_files_only_in_ide: bool = True
    """When True (default), background IDE type-checks triggered automatically only check
    currently opened files for performance. Set to False to always check the full workspace."""


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


class TypeCheckHandler(
    code_action.ActionHandler[
        type_check_action.TypeCheckAction, TypeCheckHandlerConfig
    ]
):
    def __init__(
        self,
        config: TypeCheckHandlerConfig,
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

    async def _type_check_project(
        self,
        project_path: pathlib.Path,
        project_files: list[pathlib.Path],
        run_meta: code_action.RunActionMeta,
        progress,
        partial_result_sender,
    ) -> None:
        project_file_uris = [path_to_resource_uri(f) for f in project_files]
        results = await self.workspace_action_runner.run_action_in_projects(
            action_type=TypeCheckFilesAction,
            payload=DiagnosticFilesRunPayload(file_paths=project_file_uris),
            meta=run_meta,
            project_paths=[project_path],
        )
        if not results:
            self.logger.warning(
                f"TypeCheckHandler: no TypeCheckFilesAction handlers found for project '{project_path}' "
                f"— sending empty results for {len(project_files)} file(s)"
            )
            await progress.advance(steps=len(project_files), message=None)
            await partial_result_sender.send(
                type_check_action.TypeCheckRunResult(messages={uri: [] for uri in project_file_uris})
            )
            return
        for result in results.values():
            uris = list(result.messages)
            msg = str(uris[0]) if uris else None
            if len(uris) > 1:
                msg += f" and {len(uris) - 1} related"
            await progress.advance(steps=len(project_files), message=msg)
            await partial_result_sender.send(type_check_action.TypeCheckRunResult(messages=result.messages))

    async def run(
        self,
        payload: type_check_action.TypeCheckRunPayload,
        run_context: type_check_action.TypeCheckRunContext,
    ):
        run_meta = run_context.meta

        project_paths = (
            [resource_uri_to_path(uri) for uri in payload.project_paths]
            if payload.project_paths is not None
            else actionable_project_paths(await self.workspace_info_provider.get_workspace_projects())
        )

        file_uris: list[ResourceUri]
        if payload.target == type_check_action.TypeCheckTarget.FILES:
            file_uris = payload.file_paths
        elif (
            self.config.type_check_opened_files_only_in_ide
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

        if not file_uris:
            self.logger.warning(
                f"TypeCheckHandler: no files to type-check (target={payload.target}, "
                f"dev_env={run_meta.dev_env}, trigger={run_meta.trigger})"
            )
            await run_context.partial_result_sender.send(type_check_action.TypeCheckRunResult(messages={}))
            return

        file_paths = [resource_uri_to_path(u) for u in file_uris]
        files_by_project = group_files_by_project(file_paths, project_paths)

        # R-307: every requested file must be covered by a partial result
        assigned_paths = {f for project_files in files_by_project.values() for f in project_files}
        unassigned_uris = [u for u, p in zip(file_uris, file_paths) if p not in assigned_paths]
        if unassigned_uris:
            self.logger.warning(
                f"TypeCheckHandler: {len(unassigned_uris)} file(s) could not be matched to any project "
                f"and will receive empty results: "
                + ", ".join(str(u) for u in unassigned_uris[:5])
                + ("..." if len(unassigned_uris) > 5 else "")
            )

        async with run_context.progress("Type-checking files", total=len(file_uris)) as progress:
            async with asyncio.TaskGroup() as tg:
                for project_path, project_files in files_by_project.items():
                    tg.create_task(
                        self._type_check_project(
                            project_path,
                            project_files,
                            run_meta,
                            progress,
                            run_context.partial_result_sender,
                        )
                    )
            if unassigned_uris:
                await run_context.partial_result_sender.send(
                    type_check_action.TypeCheckRunResult(messages={u: [] for u in unassigned_uris})
                )
