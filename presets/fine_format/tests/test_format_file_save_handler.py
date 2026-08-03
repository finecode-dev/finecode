from __future__ import annotations

import pathlib

import pytest

from fine_format import format_file_action
from fine_format.format_file_save_handler import SaveFormatFileHandler
from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import InMemoryFileEditor, NoOpLogger

_AUTHOR = ifileeditor.FileOperationAuthor(id="test")


class _RunContextStub:
    """Only the two attributes SaveFormatFileHandler reads off the run context."""

    def __init__(
        self,
        file_editor_session: ifileeditor.IFileEditorSession,
        file_info: format_file_action.FileInfo,
    ) -> None:
        self.file_editor_session = file_editor_session
        self.file_info = file_info


async def _run_save(
    file_editor: InMemoryFileEditor,
    file_path: pathlib.Path,
    formatted_content: str,
    based_on_version: str,
    save: bool = True,
) -> format_file_action.FormatFileRunResult:
    handler = SaveFormatFileHandler(logger=NoOpLogger())
    async with file_editor.session(author=_AUTHOR) as session:
        run_context = _RunContextStub(
            file_editor_session=session,
            file_info=format_file_action.FileInfo(
                file_content=formatted_content,
                file_version=based_on_version,
            ),
        )
        return await handler.run(
            payload=format_file_action.FormatFileRunPayload(
                file_path=path_to_resource_uri(file_path),
                save=save,
            ),
            run_context=run_context,  # type: ignore[arg-type]
        )


async def _current_version(
    file_editor: InMemoryFileEditor, file_path: pathlib.Path
) -> str:
    async with file_editor.session(author=_AUTHOR) as session:
        return await session.read_file_version(file_path)


async def test_formatted_content_is_committed_when_the_file_is_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    file_editor = InMemoryFileEditor()
    file_path = (tmp_path / "subject.py").resolve()
    file_editor.seed(file_path, "x=1\n")

    await _run_save(
        file_editor,
        file_path,
        formatted_content="x = 1\n",
        based_on_version=await _current_version(file_editor, file_path),
    )

    assert file_editor.contents(file_path) == "x = 1\n"
    assert file_editor.writes == [(file_path, "x = 1\n")]


async def test_commit_is_refused_when_the_file_changed_since_it_was_read(
    tmp_path: pathlib.Path,
) -> None:
    """A concurrent write must not be overwritten by output derived from stale input.

    The formatter's content was computed from the version it read. If the file
    moved on, writing that content would discard whatever produced the newer
    version, so the write is skipped rather than applied last-writer-wins.
    """
    file_editor = InMemoryFileEditor()
    file_path = (tmp_path / "subject.py").resolve()
    file_editor.seed(file_path, "x=1\n")
    stale_version = await _current_version(file_editor, file_path)

    # someone else edits the file while formatting is in flight
    file_editor.seed(file_path, "y = 2\n")

    await _run_save(
        file_editor,
        file_path,
        formatted_content="x = 1\n",
        based_on_version=stale_version,
    )

    assert file_editor.contents(file_path) == "y = 2\n"
    assert file_editor.writes == []


async def test_nothing_is_written_when_save_is_not_requested(
    tmp_path: pathlib.Path,
) -> None:
    """`save=False` is a hard invariant of the format_file contract."""
    file_editor = InMemoryFileEditor()
    file_path = (tmp_path / "subject.py").resolve()
    file_editor.seed(file_path, "x=1\n")

    result = await _run_save(
        file_editor,
        file_path,
        formatted_content="x = 1\n",
        based_on_version=await _current_version(file_editor, file_path),
        save=False,
    )

    assert file_editor.contents(file_path) == "x=1\n"
    assert file_editor.writes == []
    assert result.code == "x = 1\n"


async def test_version_conflict_surfaces_as_a_typed_error_from_the_editor(
    tmp_path: pathlib.Path,
) -> None:
    """The handler's tolerance is its own choice; the editor still refuses loudly."""
    file_editor = InMemoryFileEditor()
    file_path = (tmp_path / "subject.py").resolve()
    file_editor.seed(file_path, "x=1\n")
    stale_version = await _current_version(file_editor, file_path)
    file_editor.seed(file_path, "y = 2\n")

    async with file_editor.session(author=_AUTHOR) as session:
        with pytest.raises(ifileeditor.FileVersionConflict):
            await session.save_file(
                file_path=file_path,
                file_content="x = 1\n",
                if_version=stale_version,
            )
