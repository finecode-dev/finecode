import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    Repository,
    RepositoryCredentials,
)


@dataclasses.dataclass
class InitRepositoryProviderRunPayload(code_action.RunActionPayload):
    repositories: list[Repository]
    credentials_by_repository: dict[str, RepositoryCredentials]


class InitRepositoryProviderRunContext(
    code_action.RunActionContext[InitRepositoryProviderRunPayload]
): ...


@dataclasses.dataclass
class InitRepositoryProviderRunResult(code_action.RunActionResult):
    initialized_repositories: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, InitRepositoryProviderRunResult):
            return
        self.initialized_repositories = other.initialized_repositories

    def to_text(self) -> str | textstyler.StyledText:
        if self.initialized_repositories:
            return (
                f"Initialized repositories: {', '.join(self.initialized_repositories)}"
            )
        return "No repositories initialized"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class InitRepositoryProviderAction(
    code_action.Action[
        InitRepositoryProviderRunPayload,
        InitRepositoryProviderRunContext,
        InitRepositoryProviderRunResult,
    ]
):
    PAYLOAD_TYPE = InitRepositoryProviderRunPayload
    RUN_CONTEXT_TYPE = InitRepositoryProviderRunContext
    RESULT_TYPE = InitRepositoryProviderRunResult
