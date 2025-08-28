import dataclasses
import pathlib
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class Dependency:
    name: str
    version_or_source: str
    editable: bool = False


@dataclasses.dataclass
class InstallDepsInEnvRunPayload(code_action.RunActionPayload):
    env_name: str
    venv_dir_path: pathlib.Path
    project_dir_path: pathlib.Path
    dependencies: list[Dependency]


class InstallDepsInEnvRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
    ) -> None:
        super().__init__(run_id=run_id)


@dataclasses.dataclass
class InstallDepsInEnvRunResult(code_action.RunActionResult):
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, InstallDepsInEnvRunResult):
            return
        self.errors += other.errors

    def to_text(self) -> str | textstyler.StyledText:
        return "\n".join(self.errors)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if len(self.errors) == 0:
            return code_action.RunReturnCode.SUCCESS
        else:
            return code_action.RunReturnCode.ERROR


class InstallDepsInEnvAction(code_action.Action):
    PAYLOAD_TYPE = InstallDepsInEnvRunPayload
    RUN_CONTEXT_TYPE = InstallDepsInEnvRunContext
    RESULT_TYPE = InstallDepsInEnvRunResult
