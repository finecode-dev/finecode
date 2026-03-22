import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class EnvInfo:
    name: str
    venv_dir_path: ResourceUri
    project_def_path: ResourceUri


@dataclasses.dataclass
class CreateEnvsRunPayload(code_action.RunActionPayload):
    envs: list[EnvInfo] = dataclasses.field(default_factory=list)
    """Explicit list of environments to create. Empty means handlers discover envs."""
    recreate: bool = False
    """Remove and recreate existing environments from scratch even if they are already valid."""


class CreateEnvsRunContext(code_action.RunActionContext[CreateEnvsRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: CreateEnvsRunPayload,
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

    async def init(self) -> None:
        if self.initial_payload.envs:
            self.envs = list(self.initial_payload.envs)


@dataclasses.dataclass
class CreateEnvsRunResult(code_action.RunActionResult):
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, CreateEnvsRunResult):
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


class CreateEnvsAction(
    code_action.Action[
        CreateEnvsRunPayload, CreateEnvsRunContext, CreateEnvsRunResult
    ]
):
    """Create environments for the workspace(without installing dependencies, only environment)."""

    PAYLOAD_TYPE = CreateEnvsRunPayload
    RUN_CONTEXT_TYPE = CreateEnvsRunContext
    RESULT_TYPE = CreateEnvsRunResult
