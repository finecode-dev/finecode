from __future__ import annotations

import asyncio
import pathlib

import pytest
from loguru import logger

from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_runner.impls.file_editor import FileEditor
from finecode_extension_runner.impls.file_manager import FileManager

_AUTHOR = ifileeditor.FileOperationAuthor(id="test")


def _editor() -> FileEditor:
    return FileEditor(logger=logger, file_manager=FileManager(logger=logger))


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


async def test_read_nested_inside_a_modification_of_the_same_file_does_not_deadlock(
    tmp_path: pathlib.Path,
) -> None:
    """The shape that used to hang the ER: a modifier's own call stack reads the file it claimed.

    Formatting a project's `pyproject.toml` claims that file, then detects its
    language, which consults package layout, which reads the very same
    `pyproject.toml` through a session of its own. When reads waited on claims,
    that read waited for a release only its own caller could perform. Reads are
    shared and unconditional, so the nesting is simply not a conflict.
    """
    editor = _editor()
    file_path = tmp_path / "pyproject.toml"
    file_path.write_text('[project]\nname = "x"\n', encoding="utf-8")

    async with editor.session(author=_AUTHOR) as modifier:
        async with modifier.modify_file(file_path) as claimed:
            # a *different* session, as a DI-resolved service would use
            async with editor.session(author=_AUTHOR) as reader:
                async with asyncio.timeout(2):
                    async with reader.read_file(file_path) as read:
                        assert read.content == claimed.content
                    assert await reader.read_file_version(file_path) == claimed.version


async def test_second_modifier_of_one_file_waits_for_the_first(
    tmp_path: pathlib.Path,
) -> None:
    """Modifiers of the same path exclude each other — the guarantee that is kept."""
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")
    order: list[str] = []

    async def modify(label: str, hold: float) -> None:
        async with editor.session(author=_AUTHOR) as session:
            async with session.modify_file(file_path):
                order.append(f"{label}-enter")
                await asyncio.sleep(hold)
                order.append(f"{label}-exit")

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(modify("first", 0.05))
            await asyncio.sleep(0.01)  # let `first` claim it
            tg.create_task(modify("second", 0))

    assert order == ["first-enter", "first-exit", "second-enter", "second-exit"]


async def test_modifiers_of_different_files_do_not_wait_for_each_other(
    tmp_path: pathlib.Path,
) -> None:
    """Exclusion is per path, so one session can drive many concurrent modifications.

    This is what lets a batch format share a single session across its per-file
    tasks, which session-keyed exclusion would have serialized.
    """
    editor = _editor()
    first_file = tmp_path / "a.py"
    second_file = tmp_path / "b.py"
    for file_path in (first_file, second_file):
        file_path.write_text("x = 1\n", encoding="utf-8")

    both_claimed = asyncio.Event()
    claimed_count = 0

    async with editor.session(author=_AUTHOR) as shared_session:

        async def claim(file_path: pathlib.Path) -> None:
            nonlocal claimed_count
            async with shared_session.modify_file(file_path):
                claimed_count += 1
                if claimed_count == 2:
                    both_claimed.set()
                await asyncio.wait_for(both_claimed.wait(), timeout=2)

        async with asyncio.timeout(5):
            async with asyncio.TaskGroup() as tg:
                tg.create_task(claim(first_file))
                tg.create_task(claim(second_file))


async def test_version_checked_save_is_refused_when_the_file_changed(
    tmp_path: pathlib.Path,
) -> None:
    """A write whose basis went stale is refused, not applied over the newer content."""
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with session.read_file(file_path) as file_info:
            stale_version = file_info.version

        await session.save_file(file_path=file_path, file_content="x = 2\n")

        with pytest.raises(ifileeditor.FileVersionConflict) as exc_info:
            await session.save_file(
                file_path=file_path,
                file_content="x = 999\n",
                if_version=stale_version,
            )

        assert exc_info.value.expected_version == stale_version
        assert file_path.read_text(encoding="utf-8") == "x = 2\n"


async def test_version_checked_save_succeeds_when_the_file_is_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with session.modify_file(file_path) as file_info:
            await session.save_file(
                file_path=file_path,
                file_content="x = 2\n",
                if_version=file_info.version,
            )

    assert file_path.read_text(encoding="utf-8") == "x = 2\n"


async def test_ending_a_session_releases_the_claims_it_still_holds(
    tmp_path: pathlib.Path,
) -> None:
    """Session teardown must not leave a path permanently claimed.

    Session-scoped release is why claims still record their owning session, even
    though exclusion itself is keyed by path.
    """
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as abandoning_session:
        # enter the claim without exiting it, then let the session close
        claim = abandoning_session.modify_file(file_path)
        await claim.__aenter__()

    async with editor.session(author=_AUTHOR) as later_session:
        async with asyncio.timeout(2):
            async with later_session.modify_file(file_path) as file_info:
                assert file_info.content == "x = 1\n"
