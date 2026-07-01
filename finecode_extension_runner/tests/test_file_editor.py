from __future__ import annotations

import pathlib

import pytest
from loguru import logger

from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_runner.impls.file_editor import FileEditor
from finecode_extension_runner.impls.file_manager import FileManager

pytestmark = pytest.mark.anyio


async def test_open_file_seeds_content_for_a_file_deleted_from_disk(
    tmp_path: pathlib.Path,
) -> None:
    """`didOpen` for a file with no filesystem counterpart must not crash the ER.

    An IDE tab can outlive the file it points at (e.g. the file was deleted
    from another tool while the tab stayed open). Because the wire
    notification already carries the client's buffer content, opening must
    succeed using that content instead of failing on a disk read.
    """
    editor = FileEditor(logger=logger, file_manager=FileManager(logger=logger))
    deleted_file = tmp_path / "deleted.py"
    assert not deleted_file.exists()

    async with editor.session(author=ifileeditor.FileOperationAuthor(id="test")) as session:
        await session.open_file(file_path=deleted_file, content="print(1)\n")

        async with session.read_file(deleted_file) as file_info:
            assert file_info.content == "print(1)\n"


async def test_change_file_with_unchanged_content_does_not_notify_subscribers(
    tmp_path: pathlib.Path,
) -> None:
    """A change that reproduces the file's existing content must not broadcast a FileChangeEvent.

    IDE clients resync already-open documents after reconnecting (e.g. the
    extension runner restarting while files are still open) by re-sending a
    didChange whose content is identical to what the server already has.
    Broadcasting that as a real FileChangeEvent makes consumers that forward it to
    external LSP servers (e.g. the pyrefly bridge) look like a genuine edit
    happened, which then cancels any request already in flight for the document
    even though nothing was actually edited.
    """
    editor = FileEditor(logger=logger, file_manager=FileManager(logger=logger))
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"

    async with editor.session(author=ifileeditor.FileOperationAuthor(id="test")) as session:
        await session.open_file(file_path=file_path, content=content)

        async with session.subscribe_to_all_events() as events:
            await session.change_file(
                file_path=file_path,
                change=ifileeditor.FileChangeFull(text=content),
            )
            await session.change_file(
                file_path=file_path,
                change=ifileeditor.FileChangeFull(text="x = 2\n"),
            )

            received = await events.__anext__()
            assert isinstance(received, ifileeditor.FileChangeEvent)
            assert received.change.text == "x = 2\n"
            assert events._queue.qsize() == 0
