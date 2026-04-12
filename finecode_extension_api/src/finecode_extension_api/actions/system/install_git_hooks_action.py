# docs: docs/reference/actions.md
import dataclasses
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action


@dataclasses.dataclass
class InstallGitHooksRunPayload(code_action.RunActionPayload):
    hook_types: list[str] = dataclasses.field(default_factory=lambda: ["pre-commit"])
    """Git hook types to install."""
    force: bool = False
    """Overwrite existing hook files."""


@dataclasses.dataclass
class InstallGitHooksRunResult(code_action.RunActionResult):
    installed_hooks: list[str] = dataclasses.field(default_factory=list)
    """Hook types that were successfully installed."""
    skipped_hooks: list[str] = dataclasses.field(default_factory=list)
    """Hook types skipped because a hook already exists (and force=False)."""
    skip_reason: str | None = None
    """Set when the project is a no-op for this action (e.g. not a git repository root)."""

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, InstallGitHooksRunResult):
            return
        self.installed_hooks.extend(other.installed_hooks)
        self.skipped_hooks.extend(other.skipped_hooks)
        if other.skip_reason is not None:
            self.skip_reason = other.skip_reason

    def to_text(self) -> str:
        if (
            self.skip_reason is not None
            and not self.installed_hooks
            and not self.skipped_hooks
        ):
            return f"Skipped: {self.skip_reason}"
        lines = []
        for hook in self.installed_hooks:
            lines.append(f"Installed: {hook}")
        for hook in self.skipped_hooks:
            lines.append(f"Skipped (already exists): {hook}")
        return "\n".join(lines) if lines else "No hooks processed."


class InstallGitHooksRunContext(code_action.RunActionContext[InstallGitHooksRunPayload]):
    ...


class InstallGitHooksAction(
    code_action.Action[
        InstallGitHooksRunPayload, InstallGitHooksRunContext, InstallGitHooksRunResult
    ]
):
    """Install git hooks that run FineCode into the project's git repository.

    Scope: operates only on a git repository rooted at the project directory —
    a `.git` directory sitting directly in the project directory. Does not
    walk upward, so a FineCode project nested inside a larger git repository
    will not touch that outer repository.

    Non-git projects are a no-op: when no local `.git` is found, the action
    returns successfully with `skip_reason` set on the result and no hooks
    installed. This makes the action safe to include in a general preset
    that applies to projects with and without git.
    """

    PAYLOAD_TYPE = InstallGitHooksRunPayload
    RUN_CONTEXT_TYPE = InstallGitHooksRunContext
    RESULT_TYPE = InstallGitHooksRunResult
