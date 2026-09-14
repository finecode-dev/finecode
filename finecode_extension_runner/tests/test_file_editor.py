from __future__ import annotations

import asyncio
import pathlib

import pytest
from finecode_extension_api.interfaces import ifileeditor
from loguru import logger

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

    async with editor.session(
        author=ifileeditor.FileOperationAuthor(id="test")
    ) as session:
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

    async with editor.session(
        author=ifileeditor.FileOperationAuthor(id="test")
    ) as session:
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


async def test_a_task_can_claim_a_path_it_already_holds(
    tmp_path: pathlib.Path,
) -> None:
    """A writer may claim a batch of paths and then call operations that claim
    the paths they touch.

    Without this, any write method that takes its own claim is uncallable from
    inside a claim the same caller already holds: it would wait on a lock only
    it could release, and the whole operation would hang rather than fail. That
    is the shape `apply_code_actions` has by construction — it claims every path
    in its batch before validating anything, then writes inside that claim.
    """
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with asyncio.timeout(2):
            async with session.modify_file(file_path) as outer:
                async with session.modify_file(file_path) as inner:
                    assert inner.content == outer.content


async def test_a_nested_claim_does_not_release_the_path_when_it_exits(
    tmp_path: pathlib.Path,
) -> None:
    """Exclusion ends with the outermost claim, not the first one to finish.

    If a nested claim released the path on its way out, the outer claim would
    keep writing while another writer believed it held the file — the exact
    interleaving claiming exists to prevent, and one that no test of the
    nesting itself would notice.
    """
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")
    order: list[str] = []
    inner_exited = asyncio.Event()

    async def holder() -> None:
        async with editor.session(author=_AUTHOR) as session:
            async with session.modify_file(file_path):
                order.append("outer-enter")
                async with session.modify_file(file_path):
                    order.append("inner-enter")
                order.append("inner-exit")
                inner_exited.set()
                # Long enough for `waiter` to enter if the lock were free.
                await asyncio.sleep(0.05)
                order.append("outer-exit")

    async def waiter() -> None:
        await inner_exited.wait()
        async with editor.session(author=_AUTHOR) as session:
            async with session.modify_file(file_path):
                order.append("waiter-enter")

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(holder())
            tg.create_task(waiter())

    assert order == [
        "outer-enter",
        "inner-enter",
        "inner-exit",
        "outer-exit",
        "waiter-enter",
    ]


async def test_a_second_task_sharing_the_session_still_waits_for_the_claim(
    tmp_path: pathlib.Path,
) -> None:
    """Reentrancy is keyed on the task, never on the session.

    One session can span many independent concurrent operations, so granting a
    claim on session identity would let two genuinely concurrent writers of one
    path run at once while both believed they held it. A task, by contrast, runs
    one coroutine stack at a time: a claim reached from inside another claim of
    the same task is nested, never concurrent.
    """
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")
    order: list[str] = []
    first_claimed = asyncio.Event()

    async with editor.session(author=_AUTHOR) as shared_session:

        async def first() -> None:
            async with shared_session.modify_file(file_path):
                order.append("first-enter")
                first_claimed.set()
                await asyncio.sleep(0.05)
                order.append("first-exit")

        async def second() -> None:
            await first_claimed.wait()
            async with shared_session.modify_file(file_path):
                order.append("second-enter")

        async with asyncio.timeout(5):
            async with asyncio.TaskGroup() as tg:
                tg.create_task(first())
                tg.create_task(second())

    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_a_nested_claim_through_a_different_session_is_still_reentrant(
    tmp_path: pathlib.Path,
) -> None:
    """Reentrancy follows the task across session boundaries.

    A handler claiming a file may call into a DI-resolved service that opens a
    session of its own and modifies the same path. That call is still nested
    inside the outer claim — the task cannot be anywhere else — so keying
    reentrancy on the session instead of the task would deadlock it, which is
    the same trap `read_file` documents for reads.
    """
    editor = _editor()
    file_path = tmp_path / "pyproject.toml"
    file_path.write_text('[project]\nname = "x"\n', encoding="utf-8")

    async with editor.session(author=_AUTHOR) as outer_session:
        async with asyncio.timeout(2):
            async with outer_session.modify_file(file_path) as outer:
                async with editor.session(author=_AUTHOR) as inner_session:
                    async with inner_session.modify_file(file_path) as inner:
                        assert inner.content == outer.content


async def test_claim_file_yields_none_where_modify_file_raises(
    tmp_path: pathlib.Path,
) -> None:
    """The two write claims must disagree only about absence, not about the
    claim itself: an absent path yields no content under one and a loud error
    under the other, so a caller cannot accidentally read a plausible-looking
    empty file."""
    editor = _editor()
    missing = tmp_path / "never_existed.py"

    async with editor.session(author=_AUTHOR) as session:
        async with asyncio.timeout(2):
            async with session.claim_file(missing) as claimed:
                assert claimed is None

        with pytest.raises(ifileeditor.FileNotFound):
            async with session.modify_file(missing):
                pass


async def test_claim_file_excludes_a_concurrent_claimant(
    tmp_path: pathlib.Path,
) -> None:
    """A claim taken for a file that may not exist yet must still exclude
    another writer of that path — the whole point of the claim is to serialize
    two creators, so it is as exclusive as ``modify_file``."""
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")
    order: list[str] = []

    async def hold() -> None:
        async with editor.session(author=_AUTHOR) as session:
            async with session.claim_file(file_path):
                order.append("first-enter")
                await asyncio.sleep(0.05)
                order.append("first-exit")

    async def wait() -> None:
        async with editor.session(author=_AUTHOR) as session:
            async with session.claim_file(file_path):
                order.append("second-enter")

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(hold())
            await asyncio.sleep(0.01)
            tg.create_task(wait())

    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_create_file_refuses_an_existing_path_without_overwrite(
    tmp_path: pathlib.Path,
) -> None:
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("original\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        with pytest.raises(ifileeditor.FileAlreadyExists):
            await session.create_file(file_path, "replacement\n")

    assert file_path.read_text(encoding="utf-8") == "original\n"


async def test_delete_file_refuses_a_stale_version_and_keeps_the_file(
    tmp_path: pathlib.Path,
) -> None:
    """A delete based on content that has since changed must be refused, not
    carried out — the guard is the same one a save uses, and the file surviving
    is the observable contract."""
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with session.read_file(file_path) as file_info:
            stale_version = file_info.version
        await session.save_file(file_path, "x = 2\n")

        with pytest.raises(ifileeditor.FileVersionConflict):
            await session.delete_file(file_path, if_version=stale_version)

    assert file_path.read_text(encoding="utf-8") == "x = 2\n"


async def test_renaming_an_open_file_moves_the_tracked_content(
    tmp_path: pathlib.Path,
) -> None:
    """An open file's in-memory content is not lost to the disk copy when the
    file is renamed: a read through the new path must still see what the editor
    was tracking."""
    editor = _editor()
    old_path = tmp_path / "old.py"
    new_path = tmp_path / "new.py"
    old_path.write_text("disk content\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        await session.open_file(old_path, "tracked content\n")
        await session.rename_file(old_path, new_path)

        async with session.read_file(new_path) as file_info:
            assert file_info.content == "tracked content\n"

    assert new_path.read_text(encoding="utf-8") == "disk content\n"


async def test_each_file_operation_reaches_all_events_subscribers(
    tmp_path: pathlib.Path,
) -> None:
    """Create, delete and rename are three different facts, and a subscriber
    watching all events must be able to tell them apart — a rename reported as
    a close would send a downstream server the wrong signal about a user's
    file."""
    editor = _editor()
    created = tmp_path / "created.py"
    renamed = tmp_path / "renamed.py"
    renamed.write_text("x = 1\n", encoding="utf-8")
    target = tmp_path / "target.py"

    async with editor.session(author=_AUTHOR) as session:
        async with session.subscribe_to_all_events() as events:
            await session.create_file(created, "new\n")
            await session.delete_file(created)
            await session.rename_file(renamed, target)

            assert isinstance(await events.__anext__(), ifileeditor.FileCreateEvent)
            assert isinstance(await events.__anext__(), ifileeditor.FileDeleteEvent)
            assert isinstance(await events.__anext__(), ifileeditor.FileRenameEvent)


async def test_create_file_completes_inside_a_claim_on_the_same_path(
    tmp_path: pathlib.Path,
) -> None:
    """Creating a file from inside a claim the caller already holds on that
    path must not hang: the write method claims what it touches, and that inner
    claim is the same task's own claim rather than a second lock it can never
    acquire."""
    editor = _editor()
    missing = tmp_path / "new.py"

    async with editor.session(author=_AUTHOR) as session:
        async with asyncio.timeout(2):
            async with session.claim_file(missing) as claimed:
                assert claimed is None
                await session.create_file(missing, "new content\n")

    assert missing.read_text(encoding="utf-8") == "new content\n"


async def test_delete_file_completes_inside_a_claim_on_the_same_path(
    tmp_path: pathlib.Path,
) -> None:
    editor = _editor()
    file_path = tmp_path / "subject.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with asyncio.timeout(2):
            async with session.modify_file(file_path) as claimed:
                await session.delete_file(file_path, if_version=claimed.version)

    assert not file_path.exists()


async def test_rename_file_completes_inside_claims_on_both_paths(
    tmp_path: pathlib.Path,
) -> None:
    editor = _editor()
    old_path = tmp_path / "old.py"
    new_path = tmp_path / "new.py"
    old_path.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        async with asyncio.timeout(2):
            async with session.modify_file(old_path) as claimed:
                await session.rename_file(
                    old_path, new_path, if_version=claimed.version
                )

    assert not old_path.exists()
    assert new_path.read_text(encoding="utf-8") == "x = 1\n"


async def test_file_exists_counts_a_file_open_but_never_saved(
    tmp_path: pathlib.Path,
) -> None:
    """A file that only exists as tracked editor state is still a file for the
    purposes of a dry run: it must not be reported absent just because it has
    not reached disk yet."""
    editor = _editor()
    file_path = tmp_path / "unsaved.py"

    async with editor.session(author=_AUTHOR) as session:
        await session.open_file(file_path, "x = 1\n")
        assert await session.file_exists(file_path) is True

    assert not file_path.exists()


async def test_recursive_delete_refuses_a_version_guard(
    tmp_path: pathlib.Path,
) -> None:
    """A directory has no content hash, so asking to guard a recursive delete
    with one is a contract error that must be loud rather than silently
    ignored."""
    editor = _editor()
    directory = tmp_path / "pkg"
    directory.mkdir()

    async with editor.session(author=_AUTHOR) as session:
        with pytest.raises(ValueError):
            await session.delete_file(
                directory, recursive=True, if_version="some-version"
            )

    assert directory.exists()


async def test_recursive_delete_removes_opened_files_beneath_the_directory(
    tmp_path: pathlib.Path,
) -> None:
    """Deleting a directory must also drop every opened-file entry beneath it,
    or a later read would keep serving content for a path that no longer
    exists."""
    editor = _editor()
    directory = tmp_path / "pkg"
    directory.mkdir()
    module = directory / "module.py"
    module.write_text("x = 1\n", encoding="utf-8")

    async with editor.session(author=_AUTHOR) as session:
        await session.open_file(module, "tracked content\n")
        async with session.subscribe_to_all_events() as events:
            await session.delete_file(directory, recursive=True)

            event = await events.__anext__()
            assert isinstance(event, ifileeditor.FileDeleteEvent)
            assert event.file_path == directory

        assert not directory.exists()
        assert editor.get_opened_files() == []


async def test_write_operations_agree_with_file_exists_for_an_open_absent_file(
    tmp_path: pathlib.Path,
) -> None:
    """One notion of existence, shared by every operation on the session.

    An IDE tab can outlive the file it points at, so the editor tracks content
    for a path with no filesystem counterpart. If the write operations asked
    the filesystem directly while `file_exists` consulted the tracked buffers,
    a session would contradict itself at one instant: creating would clobber a
    buffer it should have refused, and deleting would refuse a file the editor
    is holding. Consulting the storage layer directly would also strand every
    non-local `IFileManager` backend.
    """
    editor = _editor()
    ghost = (tmp_path / "ghost.py").resolve()

    async with editor.session(author=_AUTHOR) as session:
        await session.open_file(file_path=ghost, content="print(1)\n")
        assert await session.file_exists(ghost) is True

        # Creating over it is refused, and the tracked content is untouched.
        with pytest.raises(ifileeditor.FileAlreadyExists):
            await session.create_file(ghost, "clobbered\n")
        async with session.read_file(ghost) as file_info:
            assert file_info.content == "print(1)\n"

        # Renaming it away is possible: the editor holds it, so it exists.
        moved = (tmp_path / "moved.py").resolve()
        await session.rename_file(ghost, moved)
        assert await session.file_exists(moved) is True

        # And deleting it is possible for the same reason.
        await session.delete_file(moved)
        assert await session.file_exists(moved) is False
