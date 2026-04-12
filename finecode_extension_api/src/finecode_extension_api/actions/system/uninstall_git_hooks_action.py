# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action


@dataclasses.dataclass
class UninstallGitHooksRunPayload(code_action.RunActionPayload):
    hook_types: list[str] = dataclasses.field(default_factory=lambda: ["pre-commit"])
    """Git hook types to uninstall."""


@dataclasses.dataclass
class UninstallGitHooksRunResult(code_action.RunActionResult):
    removed_hooks: list[str] = dataclasses.field(default_factory=list)
    """Hook types whose files were deleted."""
    skipped_hooks: list[str] = dataclasses.field(default_factory=list)
    """Hook types with no hook file present (already uninstalled)."""
    refused_hooks: list[str] = dataclasses.field(default_factory=list)
    """Hook types whose file exists but was not installed by FineCode — not deleted."""
    skip_reason: str | None = None
    """Set when the project is a no-op for this action (e.g. not a git repository root)."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, UninstallGitHooksRunResult):
            return
        self.removed_hooks.extend(other.removed_hooks)
        self.skipped_hooks.extend(other.skipped_hooks)
        self.refused_hooks.extend(other.refused_hooks)
        if other.skip_reason is not None:
            self.skip_reason = other.skip_reason

    def to_text(self) -> str:
        if (
            self.skip_reason is not None
            and not self.removed_hooks
            and not self.skipped_hooks
            and not self.refused_hooks
        ):
            return f"Skipped: {self.skip_reason}"
        lines = []
        for hook in self.removed_hooks:
            lines.append(f"Removed: {hook}")
        for hook in self.skipped_hooks:
            lines.append(f"Skipped (not installed): {hook}")
        for hook in self.refused_hooks:
            lines.append(f"Refused (not managed by FineCode): {hook}")
        return "\n".join(lines) if lines else "No hooks processed."


class UninstallGitHooksRunContext(
    code_action.RunActionContext[UninstallGitHooksRunPayload]
):
    ...


class UninstallGitHooksAction(
    code_action.Action[
        UninstallGitHooksRunPayload,
        UninstallGitHooksRunContext,
        UninstallGitHooksRunResult,
    ]
):
    """Remove FineCode-managed git hooks from the project's git repository.

    Scope: operates only on a git repository rooted at the project directory —
    a `.git` directory sitting directly in the project directory. Does not
    walk upward, so a FineCode project nested inside a larger git repository
    will not touch that outer repository.

    Non-git projects are a no-op: when no local `.git` is found, the action
    returns successfully with `skip_reason` set on the result and no hooks
    removed.

    Refuses to delete hook files that do not contain the FineCode marker —
    hooks installed by other tools or by hand are left untouched and listed
    in `refused_hooks` on the result.
    """

    PAYLOAD_TYPE = UninstallGitHooksRunPayload
    RUN_CONTEXT_TYPE = UninstallGitHooksRunContext
    RESULT_TYPE = UninstallGitHooksRunResult
