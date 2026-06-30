from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_code_hierarchy.type_hierarchy_subtypes_action import (
    TypeHierarchySubtypesPayload,
    TypeHierarchySubtypesResult,
)
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_python_lang.type_hierarchy_subtypes_python_action import (
    TypeHierarchySubtypesPythonAction,
)
from fine_python_pyrefly._lsp_hierarchy_utils import (
    type_hierarchy_item_from_lsp,
    type_hierarchy_item_to_lsp,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class PyreflyTypeHierarchySubtypesHandlerConfig(code_action.ActionHandlerConfig):
    pass


class PyreflyTypeHierarchySubtypesHandler(
    code_action.ActionHandler[
        TypeHierarchySubtypesPythonAction,
        PyreflyTypeHierarchySubtypesHandlerConfig,
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="PyreflyTypeHierarchySubtypesHandler"
    )

    def __init__(
        self,
        config: PyreflyTypeHierarchySubtypesHandlerConfig,
        file_editor: ifileeditor.IFileEditor,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: PyreflyLspService,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.file_editor = file_editor
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service
        self.logger = logger

    async def run(
        self,
        payload: TypeHierarchySubtypesPayload,
        run_context: code_action.RunActionContext,
    ) -> TypeHierarchySubtypesResult:
        file_path = resource_uri_to_path(payload.item.uri)

        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        async with self.file_editor.session(author=self.FILE_OPERATION_AUTHOR) as session:
            async with session.read_file(file_path) as file_info:
                content = file_info.content

        item_dict = type_hierarchy_item_to_lsp(payload.item)
        raw_result = await self.lsp_service.get_type_hierarchy_subtypes(
            file_path, content, item_dict
        )
        if not raw_result:
            return TypeHierarchySubtypesResult(items=[])

        return TypeHierarchySubtypesResult(
            items=[type_hierarchy_item_from_lsp(d) for d in raw_result]
        )
