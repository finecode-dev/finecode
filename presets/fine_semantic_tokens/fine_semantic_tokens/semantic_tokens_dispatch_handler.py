from __future__ import annotations

import asyncio
import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import (
    group_src_artifact_files_by_lang_action,
)
from fine_semantic_tokens import text_document_semantic_tokens_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class SemanticTokensDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class SemanticTokensDispatchHandler(
    code_action.ActionHandler[
        text_document_semantic_tokens_action.TextDocumentSemanticTokensAction,
        SemanticTokensDispatchHandlerConfig,
    ]
):
    """Dispatch ``text_document_semantic_tokens`` to language-specific subactions.

    Detects the language of the requested document via
    GroupSrcArtifactFilesByLangAction and invokes the matching language subaction
    (e.g. TextDocumentSemanticTokensPythonAction). If no subaction is registered
    for the document's language, returns an empty SemanticTokensResult.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _get_tokens_for_lang(
        self,
        subaction: iprojectactionrunner.ActionRef,
        payload: text_document_semantic_tokens_action.SemanticTokensPayload,
        meta: code_action.RunActionMeta,
        partial_result_sender: code_action.PartialResultSender,
    ) -> None:
        async for partial in self.action_runner.run_action_iter(
            action_type=subaction,
            payload=payload,
            meta=meta,
        ):
            await partial_result_sender.send(partial)

    async def run(
        self,
        payload: text_document_semantic_tokens_action.SemanticTokensPayload,
        run_context: code_action.RunActionContext,
    ) -> None:
        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            text_document_semantic_tokens_action.TextDocumentSemanticTokensAction
        )

        if not subactions_by_lang:
            self.logger.debug(
                "SemanticTokensDispatchHandler: no language subactions registered"
            )
            return

        files_by_lang_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction),
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=[payload.uri],
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )

        async with asyncio.TaskGroup() as tg:
            for lang, file_uris in files_by_lang_result.files_by_lang.items():
                if not file_uris or lang not in subactions_by_lang:
                    continue
                tg.create_task(
                    self._get_tokens_for_lang(
                        subactions_by_lang[lang],
                        payload,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
