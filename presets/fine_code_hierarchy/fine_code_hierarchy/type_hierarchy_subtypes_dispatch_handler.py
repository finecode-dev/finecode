from __future__ import annotations

import asyncio
import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import (
    group_src_artifact_files_by_lang_action,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from fine_code_hierarchy.type_hierarchy_subtypes_action import (
    TypeHierarchySubtypesAction,
    TypeHierarchySubtypesPayload,
)


@dataclasses.dataclass
class TypeHierarchySubtypesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class TypeHierarchySubtypesDispatchHandler(
    code_action.ActionHandler[
        TypeHierarchySubtypesAction,
        TypeHierarchySubtypesDispatchHandlerConfig,
    ]
):
    """Dispatch type_hierarchy_subtypes to language-specific subactions.

    Detects the language of the requested document via
    GroupSrcArtifactFilesByLangAction and invokes the matching language subaction
    (e.g. TypeHierarchySubtypesPythonAction). If no subaction is registered
    for the document's language, returns an empty result.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _dispatch_to_lang(
        self,
        subaction: iprojectactionrunner.ActionRef,
        payload: TypeHierarchySubtypesPayload,
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
        payload: TypeHierarchySubtypesPayload,
        run_context: code_action.RunActionContext,
    ) -> None:
        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            TypeHierarchySubtypesAction
        )

        if not subactions_by_lang:
            self.logger.debug(
                "TypeHierarchySubtypesDispatchHandler: no language subactions registered"
            )
            return

        files_by_lang_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction),
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=[payload.item.uri],
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )

        async with asyncio.TaskGroup() as tg:
            for lang, file_uris in files_by_lang_result.files_by_lang.items():
                if not file_uris or lang not in subactions_by_lang:
                    continue
                tg.create_task(
                    self._dispatch_to_lang(
                        subactions_by_lang[lang],
                        payload,
                        run_context.meta,
                        run_context.partial_result_sender,
                    )
                )
