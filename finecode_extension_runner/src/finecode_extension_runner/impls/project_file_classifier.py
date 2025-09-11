import enum
import pathlib

from finecode_extension_api.interfaces import iprojectfileclassifier, iprojectinfoprovider
from finecode_extension_api import service


class ProjectLayout(enum.Enum):
    SRC = enum.auto()
    FLAT = enum.auto()
    CUSTOM = enum.auto()


class ProjectFileClassifier(iprojectfileclassifier.IProjectFileClassifier, service.Service):
    # requirements:
    # - all project sources should be in a single directory
    # - if tests are outside of sources, they should be in a single directory

    def __init__(self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider) -> None:
        self.project_info_provider = project_info_provider
        # ProjectFileClassifier is instantiated as singletone, cache can be stored in
        # object
        self._file_type_by_path: dict[pathlib.Path, iprojectfileclassifier.ProjectFileType] = {}

        self.project_layout: ProjectLayout
        self.project_src_dir_path: pathlib.Path
        self.project_tests_dir_path: pathlib.Path

    async def init(self) -> None:
        project_dir_path = self.project_info_provider.get_current_project_dir_path()
        project_package_name = await self.project_info_provider.get_current_project_package_name()
        self.project_layout = self._get_project_layout(project_dir_path, project_package_name)
        if self.project_layout == ProjectLayout.SRC:
            self.project_src_dir_path = project_dir_path / 'src'
        elif self.project_layout == ProjectLayout.FLAT:
            self.project_src_dir_path = project_dir_path / project_package_name
        else:
            self.project_src_dir_path = None
        
        self.project_tests_dir_path: pathlib.Path = project_dir_path / 'tests'

    def _get_project_layout(self, project_dir_path: pathlib.Path, project_package_name: str) -> ProjectLayout:
        if (project_dir_path / 'src').exists():
            return ProjectLayout.SRC
        elif (project_dir_path / project_package_name).exists():
            return ProjectLayout.FLAT
        else:
            return ProjectLayout.CUSTOM

    def get_project_file_type(self, file_path: pathlib.Path) -> iprojectfileclassifier.ProjectFileType:
        if self.project_src_dir_path is None:
            raise NotImplementedError(f'{self.project_layout} project layout is not supported')

        if file_path in self._file_type_by_path:
            # return cached value if exist
            return self._file_type_by_path[file_path]

        if file_path.is_relative_to(self.project_src_dir_path):
            file_path_relative_to_project = file_path.relative_to(self.project_src_dir_path)
            if '__tests__' in file_path_relative_to_project.parts or 'tests' in file_path_relative_to_project.parts:
                file_type = iprojectfileclassifier.ProjectFileType.TEST
            else:
                file_type = iprojectfileclassifier.ProjectFileType.SOURCE
        else:
            # not source, check whether test
            if file_path.is_relative_to(self.project_tests_dir_path):
                file_type = iprojectfileclassifier.ProjectFileType.TEST
            else:
                file_type = iprojectfileclassifier.ProjectFileType.UNKNOWN

        # cache
        self._file_type_by_path[file_path] = file_type

        return file_type

    def get_env_for_file_type(self, file_type: iprojectfileclassifier.ProjectFileType) -> str:
        match file_type:
            case iprojectfileclassifier.ProjectFileType.SOURCE:
                return 'runtime'
            case iprojectfileclassifier.ProjectFileType.TEST:
                # TODO: dynamic. In future test tool can be installed in any env, we
                # need a way to define it in config and get it here
                # TODO: there can be also e2e tests that don't use runtime and are in
                # e.g. dev_no_runtime env
                return 'dev'
            case _:
                raise NotImplementedError("")
