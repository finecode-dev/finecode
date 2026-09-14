from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeCommandResult, FakeCommandRunner
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler

from fine_git.get_git_status_action import GetGitStatusAction, GetGitStatusRunPayload
from fine_git.git_get_git_status_handler import GitGetGitStatusHandler
from fine_git.git_types import GitChangeKind


@pytest.mark.asyncio
async def test_porcelain_output_is_parsed_including_the_two_record_rename() -> None:
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
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=status_stdout),
        ]
    )

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.repo_root == "file:///repo"
    assert result.error is None
    assert len(result.changes) == 4

    modified, staged_and_modified, untracked, renamed = result.changes

    assert modified.path == "file:///repo/modified.txt"
    assert modified.index_status == GitChangeKind.UNMODIFIED
    assert modified.worktree_status == GitChangeKind.MODIFIED
    assert modified.original_path is None

    assert staged_and_modified.path == "file:///repo/staged_and_modified.txt"
    assert staged_and_modified.index_status == GitChangeKind.MODIFIED
    assert staged_and_modified.worktree_status == GitChangeKind.MODIFIED
    assert staged_and_modified.original_path is None

    assert untracked.path == "file:///repo/untracked.txt"
    assert untracked.index_status == GitChangeKind.UNTRACKED
    assert untracked.worktree_status == GitChangeKind.UNTRACKED
    assert untracked.original_path is None

    assert renamed.path == "file:///repo/new_name.txt"
    assert renamed.index_status == GitChangeKind.RENAMED
    assert renamed.worktree_status == GitChangeKind.UNMODIFIED
    assert renamed.original_path == "file:///repo/old_name.txt"


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
async def test_empty_paths_list_skips_status_but_still_reports_repo_root() -> None:
    """`paths=[]` means nothing was requested -- the handler reports the repo root
    it already resolved without spending a `git status` call on an empty request."""
    command_runner = FakeCommandRunner(
        results=[FakeCommandResult(exit_code=0, stdout="/repo\n")]
    )

    result = await run_handler(
        GitGetGitStatusHandler,
        GetGitStatusRunPayload(paths=[]),
        action_cls=GetGitStatusAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.repo_root == "file:///repo"
    assert result.changes == []
    assert len(command_runner.commands) == 1


@pytest.mark.asyncio
async def test_include_ignored_without_untracked_asks_git_for_both_and_filters() -> (
    None
):
    """`include_ignored=True` with `include_untracked=False` is not expressible as
    git flags: `-uno --ignored=matching` is `fatal: Unsupported combination of
    ignored and untracked-files arguments`, and `-uno --ignored=traditional`
    reports no ignored files at all. The handler therefore asks with `-uall` and
    drops the untracked entries itself."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
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
        "file:///repo/build.log",
        "file:///repo/tracked.py",
    ]


@pytest.mark.asyncio
async def test_excluding_untracked_alone_still_asks_git_to_suppress_them() -> None:
    """Without `include_ignored` there is nothing to widen for, so the cheaper
    `-uno` is used and git never enumerates untracked files."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
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
) -> None:
    """`paths=None` means the project directory, not the repository. `git status`
    ignores cwd and reports the whole repository unless a pathspec says otherwise,
    so the handler must name the project directory itself -- without it, a nested
    project's status would include every other project in the same repository."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
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
    assert f"-- {tmp_path}" in status_cmd
