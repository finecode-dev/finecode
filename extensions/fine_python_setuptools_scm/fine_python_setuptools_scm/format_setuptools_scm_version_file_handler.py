import dataclasses

from fine_format import format_file_action
from fine_src_artifacts import get_src_artifact_version_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import (
    ifileeditor,
    ifilemanager,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from ._scm_config import load_configuration, resolve_def_path, version_file_path


@dataclasses.dataclass
class FormatSetuptoolsScmVersionFileHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatSetuptoolsScmVersionFileHandler(
    code_action.ActionHandler[
        get_src_artifact_version_action.GetSrcArtifactVersionAction,
        FormatSetuptoolsScmVersionFileHandlerConfig,
    ]
):
    """Format the setuptools_scm version file with the project's formatter.

    The file formatted is the ``[tool.setuptools_scm] version_file`` that the
    scm handler caused setuptools_scm to write. Which formatter runs is decided
    by ``format_file``'s dispatch on the target file, never here.

    No formatter covering the target file is not an error: the miss stays
    visible as coverage and is never absorbed. A formatter that fails does fail
    the run; disable this handler to leave the file as setuptools_scm writes
    it.

    The content is read from disk, not through the file editor, because
    setuptools_scm writes past the editor: a buffer opened in the IDE would
    still hold the previous version text, and formatting that stale content
    would save it over the fresh write.

    This handler must run after a handler that determines the version, and it
    returns that version unchanged.
    """

    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="FormatSetuptoolsScmVersionFileHandler"
    )

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        file_editor: ifileeditor.IFileEditor,
        file_manager: ifilemanager.IFileManager,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.file_editor = file_editor
        self.file_manager = file_manager
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_version_action.GetSrcArtifactVersionRunPayload,
        run_context: get_src_artifact_version_action.GetSrcArtifactVersionRunContext,
    ) -> get_src_artifact_version_action.GetSrcArtifactVersionRunResult:
        current_result = run_context.current_result
        if not isinstance(
            current_result,
            get_src_artifact_version_action.GetSrcArtifactVersionRunResult,
        ):
            raise code_action.ActionFailedException(
                "get_src_artifact_version_setuptools_scm_format: no version from"
                " a previous handler; register it after a handler that determines"
                " the version"
            )
        version = current_result.version

        def_path = resolve_def_path(
            payload.src_artifact_def_path, self.project_info_provider
        )
        config = load_configuration(def_path, self.logger)
        target = version_file_path(config, self.logger)
        if target is None or not target.is_file():
            return get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
                version=version
            )

        content = await self.file_manager.get_content(target)
        coverage: list[ItemCoverage] = []
        try:
            result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    format_file_action.FormatFileAction
                ),
                payload=format_file_action.FormatFileRunPayload(
                    file_path=path_to_resource_uri(target), save=False
                ),
                meta=run_context.meta,
                caller_kwargs=format_file_action.FormatFileCallerRunContextKwargs(
                    file_editor_session=None,  # non-serializable; must stay None
                    file_info=format_file_action.FileInfo(
                        file_content=content,
                        file_version="",
                    ),
                ),
            )
        except iprojectactionrunner.ActionNotFound:
            # No dispatcher ran to record the miss, so record it here: the
            # version file has no formatter, the same answer as "no subactions
            # registered".
            coverage = [
                ItemCoverage(
                    status=CoverageStatus.NO_SUBACTIONS,
                    item=path_to_resource_uri(target),
                    detail="format_file",
                )
            ]
            self.logger.warning(
                "get_src_artifact_version_setuptools_scm_format: no format_file"
                " action registered; leaving the version file unformatted"
            )
        except iprojectactionrunner.ActionRunFailed as exc:
            raise code_action.ActionFailedException(
                f"Formatting the version file {target} failed (disable handler"
                " 'get_src_artifact_version_setuptools_scm_format' to keep it"
                f" unformatted):\n  - {exc.message}"
            ) from exc
        else:
            if result.unhandled:
                # Not absorbed: the miss reaches this run's result through the
                # coverage sink, which is how the caller learns the file is
                # unformatted.
                self.logger.warning(
                    "get_src_artifact_version_setuptools_scm_format: no formatter"
                    " covers the version file; leaving it unformatted"
                )
            elif result.changed:
                async with self.file_editor.session(
                    author=self.FILE_OPERATION_AUTHOR
                ) as session:
                    await session.save_file(
                        file_path=target,
                        file_content=result.code,
                    )

        return get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
            version=version, coverage=coverage
        )
