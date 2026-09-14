# docs: docs/reference/actions.md
import dataclasses

import tomlkit
from finecode_extension_api.interfaces import (
    ifileeditor,
    ifilemanager,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path
from packaging.utils import canonicalize_name

from fine_envs import dump_config_action
from fine_envs.dependency_config_utils import get_dependency_name
from finecode_extension_api import code_action


@dataclasses.dataclass
class DumpConfigSaveHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigSaveHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigSaveHandlerConfig
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="DumpConfigSaveHandler")

    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
        file_editor: ifileeditor.IFileEditor,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.file_manager = file_manager
        self.file_editor = file_editor
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        raw_config_str = tomlkit.dumps(run_context.raw_config_dump)
        active_selection = (
            await self.project_info_provider.get_workspace_extra_selection()
        )
        attribution = _attribution_comment(
            active_selection, run_context.raw_config_dump
        )
        if attribution:
            raw_config_str = attribution + raw_config_str
        target_file_path = resource_uri_to_path(payload.target_file_path)
        target_file_dir_path = target_file_path.parent

        await self.file_manager.create_dir(dir_path=target_file_dir_path)
        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            await session.save_file(
                file_path=target_file_path, file_content=raw_config_str
            )

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )


def _attribution_comment(
    active_selection: dict[str, list[str]], raw_config: dict
) -> str:
    """Return a TOML comment block naming each dependency spec the workspace
    selection rewrote, or an empty string when nothing was rewritten.

    The selection is derivable at dump time from ``finecode-workspace-user.toml``
    alone, so attribution needs no provenance threaded through config merging.
    """
    if not active_selection:
        return ""
    lines: list[str] = []
    for specs in raw_config.get("dependency-groups", {}).values():
        for spec in specs:
            if not isinstance(spec, str):
                continue
            extras = active_selection.get(canonicalize_name(get_dependency_name(spec)))
            if extras:
                lines.append(
                    f"# {spec} (selected by extra(s) {', '.join(extras)} "
                    f"in finecode-workspace-user.toml)"
                )
    if not lines:
        return ""
    return (
        "# Dependency specs rewritten by finecode-workspace-user.toml:\n"
        + "\n".join(lines)
        + "\n"
    )
