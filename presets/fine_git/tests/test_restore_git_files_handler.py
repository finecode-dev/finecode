from __future__ import annotations

import pathlib

import pytest
from conftest import FakeCommandResult, FakeCommandRunner
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import run_handler

from fine_git.git_restore_git_files_handler import GitRestoreGitFilesHandler
from fine_git.restore_git_files_action import (
    RestoreGitFilesAction,
    RestoreGitFilesRunPayload,
)


@pytest.mark.asyncio
async def test_path_outside_project_dir_is_skipped_and_never_reaches_git_restore(
    tmp_path: pathlib.Path,
) -> None:
    """A caller-supplied path outside the project directory is refused before any
    git command runs -- this is the check that makes it safe to hand this action a
    caller-supplied file list without risking a restore outside the project."""
    outside_path = tmp_path.parent / "sibling" / "outside.txt"
    outside_uri = path_to_resource_uri(outside_path)
    command_runner = FakeCommandRunner(
        results=[FakeCommandResult(exit_code=0, stdout=f"{tmp_path}\n")]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[outside_uri]),
        action_cls=RestoreGitFilesAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.skipped[outside_uri] == "outside the project directory"
    assert result.restored == []
    assert all("restore" not in cmd for cmd in command_runner.commands)


@pytest.mark.asyncio
async def test_untracked_path_is_skipped_when_remove_untracked_is_false(
    tmp_path: pathlib.Path,
) -> None:
    """An untracked path is not silently deleted -- with remove_untracked=False
    (the default) it must be skipped rather than restored or removed."""
    uri = path_to_resource_uri(tmp_path / "untracked.txt")
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout=f"{tmp_path}\n"),
            FakeCommandResult(exit_code=0, stdout="?? untracked.txt\0"),
        ]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[uri], remove_untracked=False),
        action_cls=RestoreGitFilesAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.skipped[uri] == "untracked (remove_untracked is false)"
    assert result.restored == []
    assert result.removed == []
    assert all("restore" not in cmd for cmd in command_runner.commands)


@pytest.mark.asyncio
async def test_modified_tracked_path_is_restored_from_head_in_the_worktree(
    tmp_path: pathlib.Path,
) -> None:
    """A modified tracked path must be restored via `git restore --source=HEAD
    --worktree` and land in `restored`, the default (target=WORKTREE,
    source_ref=HEAD) being the common "discard my edits" case."""
    uri = path_to_resource_uri(tmp_path / "modified.txt")
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout=f"{tmp_path}\n"),
            FakeCommandResult(exit_code=0, stdout=" M modified.txt\0"),
            FakeCommandResult(exit_code=0),
        ]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[uri]),
        action_cls=RestoreGitFilesAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.restored == [uri]
    assert result.error is None
    restore_cmd = command_runner.commands[2]
    assert "--source=HEAD" in restore_cmd
    assert "--worktree" in restore_cmd


@pytest.mark.asyncio
async def test_failing_git_restore_reports_stderr_and_leaves_restored_empty(
    tmp_path: pathlib.Path,
) -> None:
    """A failing `git restore` must be reported via `error` with `restored`
    left empty, never raised, since nothing is known to have been restored."""
    uri = path_to_resource_uri(tmp_path / "modified.txt")
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout=f"{tmp_path}\n"),
            FakeCommandResult(exit_code=0, stdout=" M modified.txt\0"),
            FakeCommandResult(exit_code=1, stderr="error: pathspec did not match"),
        ]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[uri]),
        action_cls=RestoreGitFilesAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.error == "error: pathspec did not match"
    assert result.restored == []


@pytest.mark.asyncio
async def test_failing_git_status_is_reported_instead_of_a_clean_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A failing status probe must surface as `error`, not as "no changes to
    restore" for every path: an empty status output is indistinguishable from a
    clean tree, so swallowing the exit code makes a destructive action report a
    successful no-op over a question it never got an answer to."""
    uri = path_to_resource_uri(tmp_path / "modified.txt")
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout=f"{tmp_path}\n"),
            FakeCommandResult(exit_code=128, stderr="fatal: bad revision"),
        ]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[uri]),
        action_cls=RestoreGitFilesAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.error == "fatal: bad revision"
    assert result.skipped == {}
    assert result.restored == []
    assert all("restore" not in cmd for cmd in command_runner.commands)


@pytest.mark.asyncio
async def test_project_reached_through_a_symlink_still_matches_status_entries(
    tmp_path: pathlib.Path,
) -> None:
    """`git rev-parse --show-toplevel` resolves symlinks and `resource_uri_to_path`
    does not. Keyed naively, a project reached through a symlinked path (a
    dev-container mount, macOS `/tmp`) misses every status entry and reports each
    path as "no changes to restore" -- a silent no-op with a misleading reason."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    (real_root / "modified.txt").write_text("edited")
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root)

    uri = path_to_resource_uri(link_root / "modified.txt")
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout=f"{real_root}\n"),
            FakeCommandResult(exit_code=0, stdout=" M modified.txt\0"),
            FakeCommandResult(exit_code=0),
        ]
    )

    result = await run_handler(
        GitRestoreGitFilesHandler,
        RestoreGitFilesRunPayload(paths=[uri]),
        action_cls=RestoreGitFilesAction,
        project_dir=link_root,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.restored == [uri]
    assert result.skipped == {}
