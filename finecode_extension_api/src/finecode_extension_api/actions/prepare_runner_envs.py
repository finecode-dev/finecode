import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.create_envs import EnvInfo


@dataclasses.dataclass
class PrepareRunnerEnvsRunPayload(code_action.RunActionPayload):
    recreate: bool = False
    """Remove and recreate existing environments from scratch even if they are already valid."""


class PrepareRunnerEnvsRunContext(
    code_action.RunActionContext[PrepareRunnerEnvsRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: PrepareRunnerEnvsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )
        # Populated by handlers
        self.envs: list[EnvInfo] | None = None

    async def init(self) -> None:
        pass


@dataclasses.dataclass
class PrepareRunnerEnvsRunResult(code_action.RunActionResult):
    errors: list[str]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PrepareRunnerEnvsRunResult):
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


class PrepareRunnerEnvsAction(
    code_action.Action[
        PrepareRunnerEnvsRunPayload,
        PrepareRunnerEnvsRunContext,
        PrepareRunnerEnvsRunResult,
    ]
):
    """Install finecode_extension_runner in all environments."""

    PAYLOAD_TYPE = PrepareRunnerEnvsRunPayload
    RUN_CONTEXT_TYPE = PrepareRunnerEnvsRunContext
    RESULT_TYPE = PrepareRunnerEnvsRunResult
