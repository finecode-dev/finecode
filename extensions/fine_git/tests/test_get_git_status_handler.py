from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeCommandResult, FakeCommandRunner, toplevel_result
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler

from fine_git.get_git_status_action import GetGitStatusAction, GetGitStatusRunPayload
from fine_git.git_get_git_status_handler import GitGetGitStatusHandler
from fine_git.git_types import GitChangeKind


@pytest.mark.asyncio
async def test_porcelain_output_is_parsed_including_the_two_record_rename(
    repo_root: Path,
) -> None:
    """A modified file, a staged-and-modified file, an untracked file, and a rename
    (whose original path arrives as a second, unprefixed -z record) must all decode
    to the right FileStatus -- the rename encoding is the easy part to get wrong."""
    records = [
        " M modified.txt",
        "MM staged_and_modified.txt",
        "?? untracked.txt",
        "R  new_name.txt",
        "old_name.txt",
    ]
    status_stdout = "\0".join(records) + "\0"
    command_runner = FakeCommandRunner(
        results=[
            toplevel_result(repo_root),
            FakeCommandResult(exit_code=0, stdout=status_stdout),
        ]
    )

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.repo_root == repo_root.as_uri()
    assert result.error is None
    assert len(result.changes) == 4

    modified, staged_and_modified, untracked, renamed = result.changes

    assert modified.path == (repo_root / "modified.txt").as_uri()
    assert modified.index_status == GitChangeKind.UNMODIFIED
    assert modified.worktree_status == GitChangeKind.MODIFIED
    assert modified.original_path is None

    assert staged_and_modified.path == (repo_root / "staged_and_modified.txt").as_uri()
    assert staged_and_modified.index_status == GitChangeKind.MODIFIED
    assert staged_and_modified.worktree_status == GitChangeKind.MODIFIED
    assert staged_and_modified.original_path is None

    assert untracked.path == (repo_root / "untracked.txt").as_uri()
    assert untracked.index_status == GitChangeKind.UNTRACKED
    assert untracked.worktree_status == GitChangeKind.UNTRACKED
    assert untracked.original_path is None

    assert renamed.path == (repo_root / "new_name.txt").as_uri()
    assert renamed.index_status == GitChangeKind.RENAMED
    assert renamed.worktree_status == GitChangeKind.UNMODIFIED
    assert renamed.original_path == (repo_root / "old_name.txt").as_uri()


@pytest.mark.asyncio
async def test_not_a_git_repository_is_a_result_state_not_an_error() -> None:
    """A non-zero `git rev-parse` means the project is outside any repository -- this
    is reported as `repo_root=None` with no error, and `git status` is never run."""
    command_runner = FakeCommandRunner(
        results=[FakeCommandResult(exit_code=128, stderr="fatal: not a git repository")]
    )

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.repo_root is None
    assert result.changes == []
    assert result.error is None
    assert len(command_runner.commands) == 1


@pytest.mark.asyncio
async def test_empty_paths_list_skips_status_but_still_reports_repo_root(
    repo_root: Path,
) -> None:
    """`paths=[]` means nothing was requested -- the handler reports the repo root
    it already resolved without spending a `git status` call on an empty request."""
    command_runner = FakeCommandRunner(results=[toplevel_result(repo_root)])

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(paths=[]),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.repo_root == repo_root.as_uri()
    assert result.changes == []
    assert len(command_runner.commands) == 1


@pytest.mark.asyncio
async def test_include_ignored_without_untracked_asks_git_for_both_and_filters(
    repo_root: Path,
) -> None:
    """`include_ignored=True` with `include_untracked=False` is not expressible as
    git flags: `-uno --ignored=matching` is `fatal: Unsupported combination of
    ignored and untracked-files arguments`, and `-uno --ignored=traditional`
    reports no ignored files at all. The handler therefore asks with `-uall` and
    drops the untracked entries itself."""
    command_runner = FakeCommandRunner(
        results=[
            toplevel_result(repo_root),
            FakeCommandResult(
                exit_code=0, stdout="!! build.log\0?? scratch.txt\0 M tracked.py\0"
            ),
        ]
    )

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(include_untracked=False, include_ignored=True),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    status_cmd = command_runner.commands[1]
    assert "--ignored=matching" in status_cmd
    assert "-uall" in status_cmd
    assert "-uno" not in status_cmd

    assert [change.path for change in result.changes] == [
        (repo_root / "build.log").as_uri(),
        (repo_root / "tracked.py").as_uri(),
    ]


@pytest.mark.asyncio
async def test_excluding_untracked_alone_still_asks_git_to_suppress_them(
    repo_root: Path,
) -> None:
    """Without `include_ignored` there is nothing to widen for, so the cheaper
    `-uno` is used and git never enumerates untracked files."""
    command_runner = FakeCommandRunner(
        results=[
            toplevel_result(repo_root),
            FakeCommandResult(exit_code=0, stdout=""),
        ]
    )

    await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(include_untracked=False),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    status_cmd = command_runner.commands[1]
    assert "-uno" in status_cmd
    assert "--ignored=no" in status_cmd


@pytest.mark.asyncio
async def test_paths_none_scopes_status_to_the_project_directory(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    """`paths=None` means the project directory, not the repository. `git status`
    ignores cwd and reports the whole repository unless a pathspec says otherwise,
    so the handler must name the project directory itself -- without it, a nested
    project's status would include every other project in the same repository."""
    command_runner = FakeCommandRunner(
        results=[
            toplevel_result(repo_root),
            FakeCommandResult(exit_code=0, stdout=""),
        ]
    )

    await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(paths=None),
        action_cls=GetGitStatusAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    status_cmd = command_runner.commands[1]
    assert "--" in status_cmd
    assert str(tmp_path) in status_cmd
