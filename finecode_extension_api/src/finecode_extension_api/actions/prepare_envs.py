import dataclasses
import pathlib
import sys

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
class PrepareEnvsRunPayload(code_action.RunActionPayload):
    envs: list[EnvInfo]


class PrepareEnvsRunContext(code_action.RunActionContext):
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
        
    async def init(self, initial_payload: PrepareEnvsRunPayload) -> None:
        for env_info in initial_payload.envs:
            self.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = env_info.project_def_path


@dataclasses.dataclass
class PrepareEnvsRunResult(code_action.RunActionResult):
    # TODO: statuses, errors, logs?
    # TODO: return code property
    results: list[pathlib.Path]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PrepareEnvsRunResult):
            return

    def to_text(self) -> str | textstyler.StyledText:
        return ''


class PrepareEnvsAction(code_action.Action):
    PAYLOAD_TYPE = PrepareEnvsRunPayload
    RUN_CONTEXT_TYPE = PrepareEnvsRunContext
    RESULT_TYPE = PrepareEnvsRunResult
