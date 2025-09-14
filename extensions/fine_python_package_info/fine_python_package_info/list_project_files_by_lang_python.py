from finecode_extension_api.interfaces import iprojectinfoprovider, ipypackagelayoutinfoprovider
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import list_project_files_by_lang as list_project_files_by_lang_action


@dataclasses.dataclass
class ListProjectFilesByLangPythonHandlerConfig(code_action.ActionHandlerConfig): ...
# TODO: parameter for additional dirs


class ListProjectFilesByLangPythonHandler(
    code_action.ActionHandler[
        list_project_files_by_lang_action.ListProjectFilesByLangAction, ListProjectFilesByLangPythonHandlerConfig
    ]
):
    def __init__(self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider, py_package_layout_info_provider: ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider) -> None:
        self.project_info_provider = project_info_provider
        self.py_package_layout_info_provider = py_package_layout_info_provider

        self.current_project_dir_path = self.project_info_provider.get_current_project_dir_path()
        self.tests_dir_path = self.current_project_dir_path / 'tests'
        self.scripts_dir_path = self.current_project_dir_path / 'scripts'
        self.setup_py_path = self.current_project_dir_path / 'setup.py'

    async def run(
        self,
        payload: list_project_files_by_lang_action.ListProjectFilesByLangRunPayload,
        run_context: list_project_files_by_lang_action.ListProjectFilesByLangRunContext,
    ) -> list_project_files_by_lang_action.ListProjectFilesByLangRunResult:
        py_files: list[pathlib.Path] = []
        project_package_src_root_dir_path = await self.py_package_layout_info_provider.get_package_src_root_dir_path(package_dir_path=self.current_project_dir_path)
        py_files += list(project_package_src_root_dir_path.rglob('*.py'))

        if self.scripts_dir_path.exists():
            py_files += list(self.scripts_dir_path.rglob('*.py'))
        
        if self.tests_dir_path.exists():
            py_files += list(self.tests_dir_path.rglob('*.py'))

        if self.setup_py_path.exists():
            py_files.append(self.setup_py_path)

        return list_project_files_by_lang_action.ListProjectFilesByLangRunResult(
            files_by_lang={"python": py_files}
        )
