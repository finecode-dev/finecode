import asyncio
import dataclasses

from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri

from fine_lint import lint_files_action


@dataclasses.dataclass
class LintFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class LintFilesDispatchHandler(
    code_action.ActionHandler[
        lint_files_action.LintFilesAction,
        LintFilesDispatchHandlerConfig,
    ]
):
    """Dispatch ``lint_files`` to language-specific lint subactions.

    The handler groups input files by language once via
    ``GroupSrcArtifactFilesByLangAction`` and then invokes the registered
    language subaction for each non-empty language bucket.

    Language subactions run concurrently, and partial lint results are forwarded
    to the caller as they are produced.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _lint_lang(
        self,
        subaction: iprojectactionrunner.ActionRef,
        file_uris: list[ResourceUri],
        meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        async for partial in self.action_runner.run_action_iter(
            action_type=subaction,
            payload=lint_files_action.LintFilesRunPayload(file_paths=file_uris),
            meta=meta,
        ):
            await partial_result_sender.send(partial)

    async def run(
        self,
        payload: lint_files_action.LintFilesRunPayload,
        run_context: lint_files_action.LintFilesRunContext,
    ) -> None:
        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            lint_files_action.LintFilesAction
        )

        if not subactions_by_lang:
            self.logger.debug(
                "LintFilesDispatchHandler: no language subactions registered"
            )
            if payload.file_paths:
                await run_context.partial_result_sender.send(
                    lint_files_action.LintFilesRunResult(
                        messages={uri: [] for uri in payload.file_paths},
                        coverage=[
                            ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=uri)
                            for uri in payload.file_paths
                        ],
                    )
                )
            return

        # Group files by language — single pass, O(files).
        files_by_lang_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction
            ),
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=payload.file_paths,
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )
        files_by_lang = files_by_lang_result.files_by_lang

        # Files not matched by any language subaction — or matched to a language
        # without a registered subaction (e.g. "toml" when no lint_toml_files
        # subaction exists) — get an empty result so the caller can clear stale
        # IDE diagnostics for them, and so the handler always sends something.
        matched_files: set[ResourceUri] = set()
        for lang, file_uris in files_by_lang.items():
            if lang in subactions_by_lang:
                matched_files.update(file_uris)

        # The grouping handlers return one bucket per language they recognise,
        # regardless of the ``langs`` filter (advisory — pinned by a test in
        # fine_toml_lang/fine_python_lang). Inverting the buckets per file is
        # what makes coverage attributable: a file in a recognised bucket with
        # no registered subaction is NO_SUBACTION_FOR_LANGUAGE (and the bucket
        # name is the diagnosis), a file in no bucket at all is
        # NO_LANGUAGE_DETECTED.
        lang_by_file: dict[ResourceUri, str] = {
            file_uri: lang
            for lang, file_uris in files_by_lang.items()
            for file_uri in file_uris
        }
        unmatched = [f for f in payload.file_paths if f not in matched_files]
        if unmatched:
            await run_context.partial_result_sender.send(
                lint_files_action.LintFilesRunResult(
                    messages={uri: [] for uri in unmatched},
                    coverage=[
                        ItemCoverage(
                            status=(
                                CoverageStatus.NO_SUBACTION_FOR_LANGUAGE
                                if (detected := lang_by_file.get(uri)) is not None
                                else CoverageStatus.NO_LANGUAGE_DETECTED
                            ),
                            item=uri,
                            detail=detected or "",
                        )
                        for uri in unmatched
                    ],
                )
            )

        # Run all language subactions concurrently. Each streams per-file partial
        # results directly to the caller as they arrive.
        async with asyncio.TaskGroup() as tg:
            for lang, file_uris in files_by_lang.items():
                if not file_uris or lang not in subactions_by_lang:
                    continue
                tg.create_task(
                    self._lint_lang(
                        subactions_by_lang[lang],
                        file_uris,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
