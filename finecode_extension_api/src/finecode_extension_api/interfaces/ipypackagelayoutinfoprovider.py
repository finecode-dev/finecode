import enum
import pathlib
from typing import Protocol


class PyPackageLayout(enum.Enum):
    SRC = enum.auto()
    FLAT = enum.auto()
    CUSTOM = enum.auto()


class IPyPackageLayoutInfoProvider(Protocol):
    async def get_package_layout(
        self, package_dir_path: pathlib.Path
    ) -> PyPackageLayout: ...

    async def get_package_src_root_dir_path(
        self, package_dir_path: str
    ) -> pathlib.Path:
        # returns path to root directory with package sources(where main __init__.py is).
        # if you need path to directory which is added to sys.path during execution, take
        # parent of this directory.
        ...


class FailedToGetPackageLayout(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class FailedToGetPackageSrcRootDirPath(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
