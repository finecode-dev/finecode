import pathlib

from finecode_extension_api.interfaces import (
    isrcartifactfileclassifier,
    iprojectinfoprovider,
)
from finecode_extension_api import service

from fine_python_package_info import ipypackagelayoutinfoprovider


# TODO: it should be package file classifier?
# TODO: is it specific to python?
class PySrcArtifactFileClassifier(
    isrcartifactfileclassifier.ISrcArtifactFileClassifier, service.Service
):
    # requirements:
    # - all project sources should be in a single directory
    # - if tests are outside of sources, they should be in a single directory
    #
    # Note: this service classifies files in root package of the project. It means if
    #       project contains multiple packages, they will be not considered

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        py_package_layout_info_provider: ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider,
    ) -> None:
        self.project_info_provider = project_info_provider
        self.py_package_layout_info_provider = py_package_layout_info_provider
        # PySrcArtifactFileClassifier is instantiated as singletone, cache can be stored in
        # object
        self._file_type_by_path: dict[
            pathlib.Path, isrcartifactfileclassifier.SrcArtifactFileType
        ] = {}

        self.project_src_dir_path: pathlib.Path
        self.project_tests_dir_path: pathlib.Path

    async def init(self) -> None:
        project_dir_path = self.project_info_provider.get_current_project_dir_path()

        self.project_src_dir_path = (
            await self.py_package_layout_info_provider.get_package_src_root_dir_path(
                package_dir_path=project_dir_path
            )
        )
        # TODO: move to layout provider?
        self.project_tests_dir_path: pathlib.Path = project_dir_path / "tests"

    def get_src_artifact_file_type(
        self, file_path: pathlib.Path
    ) -> isrcartifactfileclassifier.SrcArtifactFileType:
        if self.project_src_dir_path is None:
            raise NotImplementedError(
                f"{self.project_layout} project layout is not supported"
            )

        if file_path in self._file_type_by_path:
            # return cached value if exist
            return self._file_type_by_path[file_path]

        if file_path.is_relative_to(self.project_src_dir_path):
            file_path_relative_to_project = file_path.relative_to(
                self.project_src_dir_path
            )
            if (
                "__tests__" in file_path_relative_to_project.parts
                or "tests" in file_path_relative_to_project.parts
            ):
                file_type = isrcartifactfileclassifier.SrcArtifactFileType.TEST
            else:
                file_type = isrcartifactfileclassifier.SrcArtifactFileType.SOURCE
        else:
            # not source, check whether test
            if file_path.is_relative_to(self.project_tests_dir_path):
                file_type = isrcartifactfileclassifier.SrcArtifactFileType.TEST
            else:
                file_type = isrcartifactfileclassifier.SrcArtifactFileType.UNKNOWN

        # cache
        self._file_type_by_path[file_path] = file_type

        return file_type

    def get_env_for_file_type(
        self, file_type: isrcartifactfileclassifier.SrcArtifactFileType
    ) -> str:
        match file_type:
            case isrcartifactfileclassifier.SrcArtifactFileType.SOURCE:
                return "runtime"
            case isrcartifactfileclassifier.SrcArtifactFileType.TEST:
                # TODO: dynamic. In future test tool can be installed in any env, we
                # need a way to define it in config and get it here
                # TODO: there can be also e2e tests that don't use runtime and are in
                # e.g. dev_no_runtime env
                return "dev"
            case _:
                raise NotImplementedError(
                    f"Source artifact file type {file_type} is not supported by PySrcArtifactFileClassifier"
                )
