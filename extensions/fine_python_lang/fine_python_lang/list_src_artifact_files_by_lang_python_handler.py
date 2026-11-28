import dataclasses
import pathlib

from fine_src_artifacts import list_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectinfoprovider,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_api.workspace_utils import (
    nested_project_dirs,
    walk_project_files,
)

from fine_python_lang import ipypackagelayoutinfoprovider


@dataclasses.dataclass
class ListSrcArtifactFilesByLangPythonHandlerConfig(code_action.ActionHandlerConfig):
    # list of relative pathes relative to project directory with additional python
    # sources if they are not in one of default pathes
    additional_dirs: list[pathlib.Path] | None = None


class ListSrcArtifactFilesByLangPythonHandler(
    code_action.ActionHandler[
        list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangAction,
        ListSrcArtifactFilesByLangPythonHandlerConfig,
    ]
):
    def __init__(
        self,
        config: ListSrcArtifactFilesByLangPythonHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        py_package_layout_info_provider: ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.py_package_layout_info_provider = py_package_layout_info_provider
        self.workspace_info_provider = workspace_info_provider
        self.logger = logger

        self.current_project_dir_path = (
            self.project_info_provider.get_current_project_dir_path()
        )
        self.tests_dir_path = self.current_project_dir_path / "tests"
        self.scripts_dir_path = self.current_project_dir_path / "scripts"
        self.setup_py_path = self.current_project_dir_path / "setup.py"

    async def run(
        self,
        payload: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload,
        run_context: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunContext,
    ) -> list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult:
        py_files: list[pathlib.Path] = []
        project_package_src_root_dir_path = (
            await self.py_package_layout_info_provider.get_package_src_root_dir_path(
                package_dir_path=self.current_project_dir_path
            )
        )
        # a project nested in this one lists its own files and lints them with its own
        # configuration, so the walks below must stop at its root
        nested_dirs = nested_project_dirs(
            self.current_project_dir_path,
            actionable_project_paths(
                await self.workspace_info_provider.get_workspace_projects()
            ),
        )

        def walk(dir_path: pathlib.Path) -> list[pathlib.Path]:
            return walk_project_files(dir_path, suffix=".py", excluded_dirs=nested_dirs)

        py_files += walk(project_package_src_root_dir_path)

        if self.scripts_dir_path.exists():
            py_files += walk(self.scripts_dir_path)

        if self.tests_dir_path.exists():
            py_files += walk(self.tests_dir_path)

        if self.setup_py_path.exists():
            py_files.append(self.setup_py_path)

        if self.config.additional_dirs is not None:
            for dir_path in self.config.additional_dirs:
                dir_absolute_path = self.current_project_dir_path / dir_path
                if not dir_absolute_path.exists():
                    self.logger.warning(
                        f"Skip {dir_path} because {dir_absolute_path} doesn't exist"
                    )
                    continue

                py_files += walk(dir_absolute_path)

        py_uris: list[ResourceUri] = [path_to_resource_uri(p) for p in py_files]
        return (
            list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult(
                files_by_lang={"python": py_uris}
            )
        )
