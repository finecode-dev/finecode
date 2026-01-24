import enum
import pathlib
import typing


class SrcArtifactFileType(enum.Enum):
    SOURCE = enum.auto()
    TEST = enum.auto()
    UNKNOWN = enum.auto()


class ISrcArtifactFileClassifier(typing.Protocol):
    def get_src_artifact_file_type(self, file_path: pathlib.Path) -> SrcArtifactFileType: ...

    def get_env_for_file_type(self, file_type: SrcArtifactFileType) -> str: ...
