import dataclasses
import pathlib
import sys
import typing

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class BuildRunPayload(code_action.RunActionPayload):
    # path to package root dir, where usually pyproject.toml is located. Note, that
    # packages can have different layouts. Use service (TODO) to get package source
    # directory path and avoid need to handle all possible cases by yourself.
    package_root_path: pathlib.Path
    build_type: typing.Literal["release"] | typing.Literal["debug"] = "release"
    # TODO: entrypoint
    # TODO: package type
    # TODO: target platform? (including version etc)


class BuildRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
    ) -> None:
        super().__init__(run_id=run_id)


@dataclasses.dataclass
class BuildRunResult(code_action.RunActionResult):
    # files/directories which are results of build
    # TODO: for better abstraction we could split build and packaging even in case of
    # wheel and sdist
    results: list[pathlib.Path]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, BuildRunResult):
            return

    def to_text(self) -> str | textstyler.StyledText:
        return ""


# general build action: any type of project should be built: library(pure and not pure python), application(both pure distributed as python package and application transformed to executable)
# concrete use cases:
# 1. Pure Python package built with `build` and `setuptools`, result: sdist and wheel.
# 2. Python Package with mypyc. TODO
# 3. Application distributed as python package: the same as use case 1.
# 4. Application compiled with Nuitka to executable.
# 5. Application packaged to executable with pyinstaller or similar tool.
#
# Customization examples:
# - Recognize constructs(syntax, imports) supported only by higher versions of python and replace them by alternatives from older python. One universal wheel will become version-specific wheel.
# - the same could be applied for platform-specific functionalities
# - optimize implementation
type BuildAction = code_action.Action[BuildRunPayload, BuildRunContext, BuildRunResult]
