"""Shared helpers for FineCode git hook handlers."""

from pathlib import Path

FINECODE_HOOK_MARKER = "# managed-by: finecode"


def resolve_project_git_dir(project_dir: Path) -> Path | None:
    """Return the `.git` directory if the project itself is a git repository root.

    Only a `.git` directory sitting directly in `project_dir` is accepted. We
    intentionally do not walk upwards: if a FineCode project is nested inside
    a larger repository, installing hooks into that parent repo would be a
    surprising side effect. `.git` files (git worktrees / submodules) are
    also not handled — callers should treat `None` as "not a plain git repo
    root" and skip as a no-op.
    """
    git_marker = project_dir / ".git"
    if git_marker.is_dir():
        return git_marker
    return None
