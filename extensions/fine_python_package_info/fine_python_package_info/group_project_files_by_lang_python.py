from finecode_extension_api.interfaces import (
    iprojectinfoprovider,
    ipypackagelayoutinfoprovider,
    ilogger,
)
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    group_project_files_by_lang as group_project_files_by_lang_action,
)


@dataclasses.dataclass
class GroupProjectFilesByLangPythonHandlerConfig(code_action.ActionHandlerConfig):
    # list of relative pathes relative to project directory with additional python
    # sources if they are not in one of default pathes
    additional_dirs: list[pathlib.Path] | None = None


class GroupProjectFilesByLangPythonHandler(
    code_action.ActionHandler[
        group_project_files_by_lang_action.GroupProjectFilesByLangAction,
        GroupProjectFilesByLangPythonHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GroupProjectFilesByLangPythonHandlerConfig,
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
        payload: group_project_files_by_lang_action.GroupProjectFilesByLangRunPayload,
        run_context: group_project_files_by_lang_action.GroupProjectFilesByLangRunContext,
    ) -> group_project_files_by_lang_action.GroupProjectFilesByLangRunResult:
        # TODO
        py_files: list[pathlib.Path] = []
        project_package_src_root_dir_path = (
            await self.py_package_layout_info_provider.get_package_src_root_dir_path(
                package_dir_path=self.current_project_dir_path
            )
        )
        py_files += list(project_package_src_root_dir_path.rglob("*.py"))

        if self.scripts_dir_path.exists():
            py_files += list(self.scripts_dir_path.rglob("*.py"))

        if self.tests_dir_path.exists():
            py_files += list(self.tests_dir_path.rglob("*.py"))

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

                py_files += list(dir_absolute_path.rglob("*.py"))

        return group_project_files_by_lang_action.GroupProjectFilesByLangRunResult(
            files_by_lang={"python": py_files}
        )
