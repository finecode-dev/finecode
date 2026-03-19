import dataclasses
import pathlib
import sys
import typing

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.create_envs import EnvInfo


@dataclasses.dataclass
class InstallEnvsRunPayload(code_action.RunActionPayload):
    envs: list[EnvInfo] = dataclasses.field(default_factory=list)
    """Explicit list of environments to install dependencies in. Empty means handlers discover envs at run time."""
    env_names: list[str] | None = None
    """Filter: when set, only in environments whose name is in this list dependencies will be installed. Applied during discovery only."""



class InstallEnvsRunContext(
    code_action.RunActionContext[InstallEnvsRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: InstallEnvsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )

        self.envs: list[EnvInfo] | None = None
        self.project_def_path_by_venv_dir_path: dict[pathlib.Path, pathlib.Path] = {}
        self.project_def_by_venv_dir_path: dict[
            pathlib.Path, dict[str, typing.Any]
        ] = {}

    async def init(self) -> None:
        self.envs = list(self.initial_payload.envs)
        for env_info in self.initial_payload.envs:
            self.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = (
                env_info.project_def_path
            )


@dataclasses.dataclass
class InstallEnvsRunResult(code_action.RunActionResult):
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, InstallEnvsRunResult):
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


class InstallEnvsAction(
    code_action.Action[
        InstallEnvsRunPayload,
        InstallEnvsRunContext,
        InstallEnvsRunResult,
    ]
):
    """Install dependencies into all environments."""

    PAYLOAD_TYPE = InstallEnvsRunPayload
    RUN_CONTEXT_TYPE = InstallEnvsRunContext
    RESULT_TYPE = InstallEnvsRunResult
