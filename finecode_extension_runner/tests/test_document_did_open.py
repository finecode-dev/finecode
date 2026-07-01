from __future__ import annotations

import pathlib
import types

import pytest
from loguru import logger

from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_runner import er_server
from finecode_extension_runner.impls.file_editor import FileEditor
from finecode_extension_runner.impls.file_manager import FileManager

pytestmark = pytest.mark.anyio


async def test_did_open_notification_seeds_content_for_a_deleted_file(
    tmp_path: pathlib.Path,
) -> None:
    """The wire `didOpen` handler must not crash on a stale IDE tab.

    Regression guard for a real incident: an IDE left a tab open for a file
    that was deleted, and the ER crashed processing `textDocument/didOpen`
    because it read the file from disk instead of using the `text` already
    included in the notification. This also guards against `text` being
    silently dropped by the wire deserializer (it previously wasn't declared
    on the ER's local param type at all).
    """
    editor = FileEditor(logger=logger, file_manager=FileManager(logger=logger))
    deleted_file = tmp_path / "deleted.py"
    assert not deleted_file.exists()

    async with editor.session(
        author=ifileeditor.FileOperationAuthor(id="FineCode_Extension_Runner_Server")
    ) as session:
        fake_server = types.SimpleNamespace(_finecode_file_editor_session=session)
        params = {
            "textDocument": {
                "uri": f"file://{deleted_file.as_posix()}",
                "languageId": "python",
                "version": 1,
                "text": "print(1)\n",
            }
        }

        await er_server._document_did_open(fake_server, params)

        async with session.read_file(deleted_file) as file_info:
            assert file_info.content == "print(1)\n"
