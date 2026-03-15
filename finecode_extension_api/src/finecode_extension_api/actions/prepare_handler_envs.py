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
class PrepareHandlerEnvsRunPayload(code_action.RunActionPayload):
    # Explicit env list. When empty, handlers should discover envs at run time.
    envs: list[EnvInfo] = dataclasses.field(default_factory=list)
    # Remove old env and create a new one from scratch even if the current one
    # is valid.
    recreate: bool = False
    # Optional filter: when set, only envs whose name is in this list are
    # prepared. Applied during discovery only — when envs is provided explicitly,
    # filter before passing.
    env_names: list[str] | None = None
    


class PrepareHandlerEnvsRunContext(
    code_action.RunActionContext[PrepareHandlerEnvsRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: PrepareHandlerEnvsRunPayload,
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
class PrepareHandlerEnvsRunResult(code_action.RunActionResult):
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PrepareHandlerEnvsRunResult):
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


class PrepareHandlerEnvsAction(
    code_action.Action[
        PrepareHandlerEnvsRunPayload,
        PrepareHandlerEnvsRunContext,
        PrepareHandlerEnvsRunResult,
    ]
):
    PAYLOAD_TYPE = PrepareHandlerEnvsRunPayload
    RUN_CONTEXT_TYPE = PrepareHandlerEnvsRunContext
    RESULT_TYPE = PrepareHandlerEnvsRunResult
