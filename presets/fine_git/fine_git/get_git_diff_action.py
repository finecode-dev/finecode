# docs: docs/reference/actions.md
import dataclasses
import enum

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri

from fine_git.git_types import GitChangeKind


class GitDiffSource(enum.StrEnum):
    WORKTREE = "worktree"
    """git diff -- unstaged changes."""
    STAGED = "staged"
    """git diff --cached -- staged changes."""
    WORKTREE_AND_HEAD = "worktree_and_head"
    """git diff HEAD -- both staged and unstaged changes, vs HEAD."""


@dataclasses.dataclass
class GetGitDiffRunPayload(code_action.RunActionPayload):
    paths: list[ResourceUri] | None = None
    """None -> the whole project directory. Empty list -> nothing requested."""
    source: GitDiffSource = GitDiffSource.WORKTREE
    context_lines: int = 3
    """Lines of context around each hunk. 0 yields hunks containing only changed
    lines, which is what callers that count added lines want."""


class GetGitDiffRunContext(code_action.RunActionContext[GetGitDiffRunPayload]): ...


@dataclasses.dataclass
class FileDiff:
    path: ResourceUri
    change_kind: GitChangeKind
    patch: str
    """The verbatim unified diff for this file, including its `diff --git` header."""
    added_lines: list[str] = dataclasses.field(default_factory=list)
    """Content of added lines, without the leading '+'. Empty for binary files."""
    removed_lines: list[str] = dataclasses.field(default_factory=list)
    """Content of removed lines, without the leading '-'. Empty for binary files."""
    original_path: ResourceUri | None = None
    is_binary: bool = False


@dataclasses.dataclass
class GetGitDiffRunResult(code_action.RunActionResult):
    repo_root: ResourceUri | None = None
    files: list[FileDiff] = dataclasses.field(default_factory=list)
    error: str | None = None

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetGitDiffRunResult):
            return

        self.repo_root = other.repo_root
        self.files = other.files
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to get git diff: {self.error}"
        if not self.files:
            return "No changes"
        return "".join(file.patch for file in self.files)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class GetGitDiffAction(
    code_action.Action[
        GetGitDiffRunPayload,
        GetGitDiffRunContext,
        GetGitDiffRunResult,
    ]
):
    """Get the git diff of paths in the project.

    Each file's result carries both the verbatim `patch` (for a human or a
    model reviewing the change) and the parsed `added_lines`/`removed_lines`
    (so mechanical consumers do not each have to write their own
    unified-diff parser). Both are produced for every file — this is one
    answer in two projections, not optional per-handler fields.
    """

    DESCRIPTION = "Get the git diff of paths in the project."
    PAYLOAD_TYPE = GetGitDiffRunPayload
    RUN_CONTEXT_TYPE = GetGitDiffRunContext
    RESULT_TYPE = GetGitDiffRunResult
