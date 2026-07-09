# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action


@dataclasses.dataclass
class SetupSystemRunPayload(code_action.RunActionPayload):
    pass


@dataclasses.dataclass
class SetupSystemRunResult(code_action.RunActionResult):
    installed: list[str] = dataclasses.field(default_factory=list)
    """Steps that completed installation or configuration."""
    skipped: list[str] = dataclasses.field(default_factory=list)
    """Steps skipped because the dependency or tool was already present."""
    failed: list[str] = dataclasses.field(default_factory=list)
    """Steps that failed. Non-empty means return_code is ERROR."""

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.failed:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, SetupSystemRunResult):
            return
        self.installed.extend(other.installed)
        self.skipped.extend(other.skipped)
        self.failed.extend(other.failed)

    def to_text(self) -> str:
        lines = [f"Installed: {s}" for s in self.installed]
        lines += [f"Skipped (already present): {s}" for s in self.skipped]
        lines += [f"Failed: {s}" for s in self.failed]
        return "\n".join(lines) if lines else "No setup steps ran."


class SetupSystemRunContext(code_action.RunActionContext[SetupSystemRunPayload]):
    ...


class SetupSystemAction(
    code_action.Action[
        SetupSystemRunPayload,
        SetupSystemRunContext,
        SetupSystemRunResult,
    ]
):
    """Run system-level setup steps registered for this project.

    Handlers can install OS packages, IDE extensions, non-Python language
    tooling, and any other dependencies that fall outside Python's package
    management and cannot be handled automatically by prepare-envs.

    Include this preset to create a named slot with an empty handler list —
    a safe no-op until handlers are registered in a shared preset, project
    config, or personal finecode-user.toml.

    Handler contract:
    - Check whether the dependency or tool is already present (idempotency).
    - Populate `skipped` when already present, `installed` on success, `failed`
      on error.
    - All handlers run regardless of prior failures; failures are collected and
      reported in aggregate.
    """

    DESCRIPTION = "Install and configure system-level dependencies and tools."
    PAYLOAD_TYPE = SetupSystemRunPayload
    RUN_CONTEXT_TYPE = SetupSystemRunContext
    RESULT_TYPE = SetupSystemRunResult
