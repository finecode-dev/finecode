# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri

from fine_git.git_types import GitChangeKind


@dataclasses.dataclass
class GetGitStatusRunPayload(code_action.RunActionPayload):
    paths: list[ResourceUri] | None = None
    """None -> the whole project directory. Empty list -> nothing was requested,
    the result is empty. Paths may be files or directories."""
    include_untracked: bool = True
    include_ignored: bool = False


class GetGitStatusRunContext(code_action.RunActionContext[GetGitStatusRunPayload]): ...


@dataclasses.dataclass
class FileStatus:
    path: ResourceUri
    index_status: GitChangeKind
    """The porcelain X column: this path's state in the index vs HEAD."""
    worktree_status: GitChangeKind
    """The porcelain Y column: this path's state in the working tree vs the index."""
    original_path: ResourceUri | None = None
    """Rename/copy source, when index_status is RENAMED or COPIED."""


@dataclasses.dataclass
class GetGitStatusRunResult(code_action.RunActionResult):
    repo_root: ResourceUri | None = None
    """None -> the project is not inside a git repository."""
    changes: list[FileStatus] = dataclasses.field(default_factory=list)
    error: str | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetGitStatusRunResult):
            return

        self.repo_root = other.repo_root
        self.changes = other.changes
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to get git status: {self.error}"
        if self.repo_root is None:
            return "Not a git repository"
        if not self.changes:
            return "No changes"

        lines = [
            f"{change.index_status}/{change.worktree_status} {change.path}"
            for change in self.changes
        ]
        count = len(self.changes)
        lines.append(f"\n{count} change{'s' if count != 1 else ''}")
        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class GetGitStatusAction(
    code_action.Action[
        GetGitStatusRunPayload,
        GetGitStatusRunContext,
        GetGitStatusRunResult,
    ]
):
    """Report the git status of paths in the project.

    Both porcelain columns (index vs HEAD, worktree vs index) are reported
    for every path rather than collapsing them into a single `staged`
    boolean, so a caller can project whichever view it needs (the staged
    set, the dirty set, the untracked set) from one complete answer instead
    of the handler guessing which view to compute.
    """

    DESCRIPTION = "Report the git status of paths in the project."
    PAYLOAD_TYPE = GetGitStatusRunPayload
    RUN_CONTEXT_TYPE = GetGitStatusRunContext
    RESULT_TYPE = GetGitStatusRunResult
