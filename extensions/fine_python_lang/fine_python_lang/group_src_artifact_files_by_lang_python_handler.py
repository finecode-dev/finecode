from finecode_extension_api.interfaces import (
    iprojectinfoprovider,
    ilogger,
)
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_lang import ipypackagelayoutinfoprovider


@dataclasses.dataclass
class GroupSrcArtifactFilesByLangPythonHandlerConfig(code_action.ActionHandlerConfig):
    # list of relative pathes relative to project directory with additional python
    # sources if they are not in one of default pathes
    additional_dirs: list[pathlib.Path] | None = None


class GroupSrcArtifactFilesByLangPythonHandler(
    code_action.ActionHandler[
        group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction,
        GroupSrcArtifactFilesByLangPythonHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GroupSrcArtifactFilesByLangPythonHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        py_package_layout_info_provider: ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.py_package_layout_info_provider = py_package_layout_info_provider
        self.logger = logger

        self.current_project_dir_path = (
            self.project_info_provider.get_current_project_dir_path()
        )
        self.tests_dir_path = self.current_project_dir_path / "tests"
        self.scripts_dir_path = self.current_project_dir_path / "scripts"
        self.setup_py_path = self.current_project_dir_path / "setup.py"

    async def run(
        self,
        payload: group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload,
        run_context: group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunContext,
    ) -> group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult:
        project_package_src_root_dir_path = (
            await self.py_package_layout_info_provider.get_package_src_root_dir_path(
                package_dir_path=self.current_project_dir_path
            )
        )

        python_dirs: list[pathlib.Path] = [project_package_src_root_dir_path]
        if self.scripts_dir_path.exists():
            python_dirs.append(self.scripts_dir_path)
        if self.tests_dir_path.exists():
            python_dirs.append(self.tests_dir_path)
        if self.config.additional_dirs is not None:
            for dir_path in self.config.additional_dirs:
                dir_absolute_path = self.current_project_dir_path / dir_path
                if not dir_absolute_path.exists():
                    self.logger.warning(
                        f"Skip {dir_path} because {dir_absolute_path} doesn't exist"
                    )
                    continue
                python_dirs.append(dir_absolute_path)

        py_uris: list[ResourceUri] = []
        for uri in payload.file_paths:
            file_path = resource_uri_to_path(uri)
            if file_path.suffix != ".py":
                continue
            if file_path == self.setup_py_path:
                py_uris.append(uri)
                continue
            if any(file_path.is_relative_to(d) for d in python_dirs):
                py_uris.append(uri)

        return group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult(
            files_by_lang={"python": py_uris}
        )
