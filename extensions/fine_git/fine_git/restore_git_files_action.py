# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


class GitRestoreTarget(enum.StrEnum):
    WORKTREE = "worktree"
    INDEX = "index"
    BOTH = "both"


@dataclasses.dataclass
class RestoreGitFilesRunPayload(code_action.RunActionPayload):
    paths: list[ResourceUri]
    """Required. There is deliberately no spelling for "restore everything":
    this action destroys uncommitted work, so every path is named explicitly."""
    target: GitRestoreTarget = GitRestoreTarget.WORKTREE
    source_ref: str = "HEAD"
    """The commit to restore file content from."""
    remove_untracked: bool = False
    """Delete listed paths that are untracked. Unrecoverable, so off by default."""


class RestoreGitFilesRunContext(
    code_action.RunActionContext[RestoreGitFilesRunPayload]
): ...


@dataclasses.dataclass
class RestoreGitFilesRunResult(code_action.RunActionResult):
    restored: list[ResourceUri] = dataclasses.field(default_factory=list)
    removed: list[ResourceUri] = dataclasses.field(default_factory=list)
    skipped: dict[ResourceUri, str] = dataclasses.field(default_factory=dict)
    """Path -> the reason it was not restored."""
    error: str | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, RestoreGitFilesRunResult):
            return

        self.restored = other.restored
        self.removed = other.removed
        self.skipped = other.skipped
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to restore files: {self.error}"

        summary = (
            f"Restored {len(self.restored)}, removed {len(self.removed)}, "
            f"skipped {len(self.skipped)}"
        )
        lines = [summary]
        for path, reason in self.skipped.items():
            lines.append(f"  skipped {path}: {reason}")
        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class RestoreGitFilesAction(
    code_action.Action[
        RestoreGitFilesRunPayload,
        RestoreGitFilesRunContext,
        RestoreGitFilesRunResult,
    ]
):
    """Restore files to their committed state, discarding local changes.

    Three safety properties keep this destructive action bounded: `paths` is
    required and has no wildcard for "restore everything"; a path outside the
    project directory is refused and reported in `skipped`, never restored;
    and deleting an untracked path is opt-in via `remove_untracked`, off by
    default because it is unrecoverable.
    """

    DESCRIPTION = "Restore files to their committed state, discarding local changes."
    PAYLOAD_TYPE = RestoreGitFilesRunPayload
    RUN_CONTEXT_TYPE = RestoreGitFilesRunContext
    RESULT_TYPE = RestoreGitFilesRunResult
