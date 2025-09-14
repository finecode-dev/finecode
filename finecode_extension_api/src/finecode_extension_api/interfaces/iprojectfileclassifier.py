import enum
import typing
import pathlib


class ProjectFileType(enum.Enum):
    SOURCE = enum.auto()
    TEST = enum.auto()
    UNKNOWN = enum.auto()


class IProjectFileClassifier(typing.Protocol):
    def get_project_file_type(self, file_path: pathlib.Path) -> ProjectFileType: ...

    def get_env_for_file_type(self, file_type: ProjectFileType) -> str: ...
