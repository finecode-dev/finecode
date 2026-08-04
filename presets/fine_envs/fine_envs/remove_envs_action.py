# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from fine_envs.create_envs_action import EnvInfo
from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class RemoveEnvsRunPayload(code_action.RunActionPayload):
    env_names: list[str] | None = None
    """Environments to remove. ``None`` means handlers discover them — which
    resolves to the project's orphaned envs. An empty list is an explicit
    no-op."""
    force: bool = False
    """Allow removing an environment that configuration still declares. Off by
    default: a declared env may have a live Extension Runner holding its
    interpreter."""


class RemoveEnvsRunContext(code_action.RunActionContext[RemoveEnvsRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: RemoveEnvsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            progress_sender=progress_sender,
        )

        self.envs: list[EnvInfo] | None = None
        """Resolved environments to remove, populated by the discovery handler.
        ``None`` means discovery has not run yet."""


@dataclasses.dataclass
class RemoveEnvsRunResult(code_action.RunActionResult):
    removed: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, RemoveEnvsRunResult):
            return
        self.removed += other.removed
        self.errors += other.errors

    @override
    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()
        if not self.removed and not self.errors:
            text.append("No environments removed.\n")
            return text

        for name in self.removed:
            text.append(f"Removed {name}\n")
        for error in self.errors:
            text.append_styled(f"{error}\n", foreground=textstyler.Color.RED)
        return text

    @property
    @override
    def return_code(self) -> code_action.RunReturnCode:
        if len(self.errors) == 0:
            return code_action.RunReturnCode.SUCCESS
        return code_action.RunReturnCode.ERROR


class RemoveEnvsAction(
    code_action.Action[RemoveEnvsRunPayload, RemoveEnvsRunContext, RemoveEnvsRunResult]
):
    """Remove environments from disk, by default the orphaned ones.

    Removal is deliberately tolerant of broken state — a half-created or
    permission-damaged venv is precisely what a user wants gone — and
    per-env failures are collected in the result rather than aborting the
    batch.
    """

    DESCRIPTION = (
        "Remove environments from disk. With no arguments, removes the"
        " project's orphaned environments — those no longer declared in"
        " configuration."
    )
    PAYLOAD_TYPE = RemoveEnvsRunPayload
    RUN_CONTEXT_TYPE = RemoveEnvsRunContext
    RESULT_TYPE = RemoveEnvsRunResult
