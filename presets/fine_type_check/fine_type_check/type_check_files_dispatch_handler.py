import asyncio
import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import group_src_artifact_files_by_lang_action
from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunResult,
    DiagnosticFilesRunContext,
)
from fine_type_check.type_check_files_action import TypeCheckFilesAction
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class TypeCheckFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class TypeCheckFilesDispatchHandler(
    code_action.ActionHandler[
        TypeCheckFilesAction,
        TypeCheckFilesDispatchHandlerConfig,
    ]
):
    """Dispatch ``type_check_files`` to language-specific type-check subactions.

    The handler groups input files by language once via
    ``GroupSrcArtifactFilesByLangAction`` and then invokes the registered
    language subaction for each non-empty language bucket.

    Language subactions run concurrently, and partial results are forwarded
    to the caller as they are produced.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _type_check_lang(
        self,
        subaction: iprojectactionrunner.ActionRef,
        file_uris: list[ResourceUri],
        meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        async for partial in self.action_runner.run_action_iter(
            action_type=subaction,
            payload=DiagnosticFilesRunPayload(file_paths=file_uris),
            meta=meta,
        ):
            await partial_result_sender.send(partial)

    async def run(
        self,
        payload: DiagnosticFilesRunPayload,
        run_context: DiagnosticFilesRunContext,
    ) -> None:
        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            TypeCheckFilesAction
        )

        if not subactions_by_lang:
            self.logger.debug("TypeCheckFilesDispatchHandler: no language subactions registered")
            if payload.file_paths:
                await run_context.partial_result_sender.send(
                    DiagnosticFilesRunResult(messages={uri: [] for uri in payload.file_paths})
                )
            return

        files_by_lang_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction),
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=payload.file_paths,
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )
        files_by_lang = files_by_lang_result.files_by_lang

        # Only files whose language has a registered subaction will be covered by
        # the dispatch loop below. Files grouped into a language without one (e.g.
        # "toml" when no type_check_toml_files subaction exists) must still get an
        # empty result here, or they get silently dropped and the handler ends up
        # sending nothing at all.
        matched_files: set[ResourceUri] = set()
        for lang, file_uris in files_by_lang.items():
            if lang in subactions_by_lang:
                matched_files.update(file_uris)

        unmatched = [f for f in payload.file_paths if f not in matched_files]
        if unmatched:
            await run_context.partial_result_sender.send(
                DiagnosticFilesRunResult(messages={uri: [] for uri in unmatched})
            )

        async with asyncio.TaskGroup() as tg:
            for lang, file_uris in files_by_lang.items():
                if not file_uris or lang not in subactions_by_lang:
                    continue
                tg.create_task(
                    self._type_check_lang(
                        subactions_by_lang[lang],
                        file_uris,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
