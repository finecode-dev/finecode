from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeCommandResult, FakeCommandRunner
from finecode_extension_api.interfaces.icommandrunner import ICommandRunner
from finecode_extension_runner.testing import run_handler

from fine_git.get_git_diff_action import (
    GetGitDiffAction,
    GetGitDiffRunPayload,
    GitDiffSource,
)
from fine_git.git_get_git_diff_handler import GitGetGitDiffHandler
from fine_git.git_types import GitChangeKind

_MODIFIED_SECTION = (
    "diff --git a/mod.txt b/mod.txt\n"
    "index abc123..def456 100644\n"
    "--- a/mod.txt\n"
    "+++ b/mod.txt\n"
    "@@ -1,1 +1,1 @@\n"
    "-old line\n"
    "+new line\n"
)

_ADDED_SECTION = (
    "diff --git a/new.txt b/new.txt\n"
    "new file mode 100644\n"
    "index 0000000..abc123\n"
    "--- /dev/null\n"
    "+++ b/new.txt\n"
    "@@ -0,0 +1,1 @@\n"
    "+new content\n"
)

_BINARY_SECTION = (
    "diff --git a/img.png b/img.png\n"
    "index 111..222 100644\n"
    "Binary files a/img.png and b/img.png differ\n"
)


@pytest.mark.asyncio
async def test_two_file_diff_parses_change_kind_and_added_removed_lines() -> None:
    """A modified file and an added file must each decode to the right
    change_kind, with added_lines/removed_lines stripped of their +/- markers
    and the +++/--- header lines excluded, while patch keeps the diff --git
    header verbatim."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=_MODIFIED_SECTION + _ADDED_SECTION),
        ]
    )

    result = await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(context_lines=0),
        action_cls=GetGitDiffAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.error is None
    assert len(result.files) == 2

    modified, added = result.files

    assert modified.path == "file:///repo/mod.txt"
    assert modified.change_kind == GitChangeKind.MODIFIED
    assert modified.added_lines == ["new line"]
    assert modified.removed_lines == ["old line"]
    assert modified.patch.startswith("diff --git a/mod.txt b/mod.txt\n")
    assert modified.is_binary is False

    assert added.path == "file:///repo/new.txt"
    assert added.change_kind == GitChangeKind.ADDED
    assert added.added_lines == ["new content"]
    assert added.removed_lines == []


@pytest.mark.asyncio
async def test_binary_diff_section_is_reported_without_added_removed_lines() -> None:
    """A `Binary files ... differ` section carries no hunks to scan for +/-
    lines, so it must set is_binary and leave added/removed lines empty
    rather than the parser tripping over the absence of a `@@` marker."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=_BINARY_SECTION),
        ]
    )

    result = await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(),
        action_cls=GetGitDiffAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert len(result.files) == 1
    file_diff = result.files[0]
    assert file_diff.is_binary is True
    assert file_diff.added_lines == []
    assert file_diff.removed_lines == []


@pytest.mark.asyncio
async def test_staged_source_and_context_lines_reach_the_diff_command() -> None:
    """`source=STAGED` must add `--cached` and `context_lines=0` must add
    `-U0` to the `git diff` invocation, or a caller's request is silently
    ignored."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=""),
        ]
    )

    await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(source=GitDiffSource.STAGED, context_lines=0),
        action_cls=GetGitDiffAction,
        service_overrides={ICommandRunner: command_runner},
    )

    diff_cmd = command_runner.commands[1]
    assert "--cached" in diff_cmd
    assert "-U0" in diff_cmd


@pytest.mark.asyncio
async def test_paths_none_scopes_diff_to_the_project_directory(
    tmp_path: Path,
) -> None:
    """`paths=None` means the project directory, not the repository. `git diff`
    ignores cwd and covers the whole repository unless a pathspec says otherwise,
    so the handler must name the project directory itself -- without it, a nested
    project's diff would include every other project in the same repository."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=""),
        ]
    )

    await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(paths=None),
        action_cls=GetGitDiffAction,
        project_dir=tmp_path,
        service_overrides={ICommandRunner: command_runner},
    )

    diff_cmd = command_runner.commands[1]
    assert f"-- {tmp_path}" in diff_cmd


_MARKDOWN_RULE_SECTION = (
    "diff --git a/doc.md b/doc.md\n"
    "index 1ac9372..ba930bd 100644\n"
    "--- a/doc.md\n"
    "+++ b/doc.md\n"
    "@@ -1,3 +1,3 @@\n"
    " title\n"
    "----\n"
    "++++more\n"
    " body\n"
)

_MODE_CHANGE_QUOTED_PATH_SECTION = (
    'diff --git "a/we\\"ird.txt" "b/we\\"ird.txt"\nold mode 100644\nnew mode 100755\n'
)


@pytest.mark.asyncio
async def test_content_lines_starting_with_dashes_or_pluses_are_not_dropped() -> None:
    """Inside a hunk, a removed `---` arrives as `----` and an added `+++more` as
    `++++more` (verified against real git). Excluding those prefixes as if they
    were file headers loses real content, and `added_lines`/`removed_lines` then
    silently disagree with `patch` -- the same answer in another projection.
    Markdown rules and YAML front matter make this an everyday case, not a
    corner one."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(exit_code=0, stdout=_MARKDOWN_RULE_SECTION),
        ]
    )

    result = await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(),
        action_cls=GetGitDiffAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert len(result.files) == 1
    assert result.files[0].removed_lines == ["---"]
    assert result.files[0].added_lines == ["+++more"]


@pytest.mark.asyncio
async def test_section_naming_no_path_is_skipped_not_fatal_for_the_whole_diff() -> None:
    """A mode-only change on a path git had to quote carries no `---`/`+++` lines
    and a header the parser cannot match (real git output). One unparseable
    section must cost that one file, not the whole answer."""
    command_runner = FakeCommandRunner(
        results=[
            FakeCommandResult(exit_code=0, stdout="/repo\n"),
            FakeCommandResult(
                exit_code=0,
                stdout=_MODE_CHANGE_QUOTED_PATH_SECTION + _MODIFIED_SECTION,
            ),
        ]
    )

    result = await run_handler(
        GitGetGitDiffHandler,
        GetGitDiffRunPayload(),
        action_cls=GetGitDiffAction,
        service_overrides={ICommandRunner: command_runner},
    )

    assert result.error is None
    assert [file.path for file in result.files] == ["file:///repo/mod.txt"]
