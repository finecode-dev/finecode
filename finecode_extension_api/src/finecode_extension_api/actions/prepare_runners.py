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
class EnvInfo:
    name: str
    venv_dir_path: pathlib.Path
    project_def_path: pathlib.Path


@dataclasses.dataclass
class PrepareRunnersRunPayload(code_action.RunActionPayload):
    envs: list[EnvInfo]
    # remove old env and create a new one from scratch even if the current one is valid.
    # Useful for example if you changed something in venv manually and want to revert
    # changes (just by running prepare it would be not solved because version of the
    # packages are the same and they are already installed)
    recreate: bool = False


class PrepareRunnersRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
    ) -> None:
        super().__init__(run_id=run_id)

        # project def pathes are stored also in context, because prepare envs can run
        # tools like pip which expected 'normalized' project definition(=without
        # additional features which finecode provides). So the usual workflow looks like
        # normalizing(dumping) configuration first and then use dumped config for
        # further handlers.
        self.project_def_path_by_venv_dir_path: dict[pathlib.Path, pathlib.Path] = {}
        # to avoid multiple writing and reading files in each action handler, save
        # modified project definition here. It also can be used as extension point if
        # for example additional dependencies should be installed by adding handler
        # which inserts them into project definition instead of modying `install_deps`
        # handler
        self.project_def_by_venv_dir_path: dict[pathlib.Path, dict[str, typing.Any]] = (
            {}
        )

    async def init(self, initial_payload: PrepareRunnersRunPayload) -> None:
        for env_info in initial_payload.envs:
            self.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = (
                env_info.project_def_path
            )


@dataclasses.dataclass
class PrepareRunnersRunResult(code_action.RunActionResult):
    # `PrepareRunners` action is general, so make result general as well
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PrepareRunnersRunResult):
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


class PrepareRunnersAction(code_action.Action):
    PAYLOAD_TYPE = PrepareRunnersRunPayload
    RUN_CONTEXT_TYPE = PrepareRunnersRunContext
    RESULT_TYPE = PrepareRunnersRunResult
