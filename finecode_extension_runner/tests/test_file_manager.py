from __future__ import annotations

import pathlib
import stat

import pytest
from finecode_extension_api.interfaces import ifilemanager
from loguru import logger

from finecode_extension_runner.impls.file_manager import FileManager

# Em dash, right double quote, é and 😀 — no newline, so the byte assertions
# below do not depend on Windows \n→\r\n translation. The right double quote
# (U+201D) and 😀 are outside cp1252, so a cp1252 read raises instead of
# mojibake, while — and é would round-trip as wrong-but-non-raising bytes.
_NON_ASCII = "x = '— ” é \U0001f600'"


def _make_dir_tree(root: pathlib.Path) -> pathlib.Path:
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x = 1\n")
    return root


async def test_remove_dir_removes_a_normal_tree(tmp_path: pathlib.Path) -> None:
    tree = _make_dir_tree(tmp_path / "tree")

    await FileManager(logger=logger).remove_dir(tree)

    assert not tree.exists()


async def test_remove_dir_raises_on_missing_path_by_default(
    tmp_path: pathlib.Path,
) -> None:
    """Without `tolerant=True`, the original `shutil.rmtree` contract holds:
    a missing path is an error, not a silent no-op."""
    with pytest.raises(ifilemanager.RemoveDirError):
        await FileManager(logger=logger).remove_dir(tmp_path / "never_existed")


async def test_remove_dir_raises_on_read_only_contents_by_default(
    tmp_path: pathlib.Path,
) -> None:
    tree = _make_dir_tree(tmp_path / "tree")
    (tree / "sub").chmod(stat.S_IRUSR | stat.S_IXUSR)

    with pytest.raises(ifilemanager.RemoveDirError):
        await FileManager(logger=logger).remove_dir(tree)

    (tree / "sub").chmod(stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)


async def test_remove_dir_tolerant_removes_read_only_contents(
    tmp_path: pathlib.Path,
) -> None:
    """A tree whose files/dirs lost write permission must still go when the
    caller specifically asked for tolerant removal."""
    tree = _make_dir_tree(tmp_path / "tree")
    locked_file = tree / "sub" / "file.txt"
    locked_file.chmod(stat.S_IRUSR)
    (tree / "sub").chmod(stat.S_IRUSR | stat.S_IXUSR)

    await FileManager(logger=logger).remove_dir(tree, tolerant=True)

    assert not tree.exists()


async def test_remove_dir_tolerant_removes_broken_tree_without_marker(
    tmp_path: pathlib.Path,
) -> None:
    tree = tmp_path / "tree"
    (tree / "bin").mkdir(parents=True)

    await FileManager(logger=logger).remove_dir(tree, tolerant=True)

    assert not tree.exists()


async def test_remove_dir_tolerant_removes_dangling_symlink(
    tmp_path: pathlib.Path,
) -> None:
    """`exists()` is False for a dangling symlink, so a naive existence check
    would leave it behind forever."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone")

    await FileManager(logger=logger).remove_dir(link, tolerant=True)

    assert not link.is_symlink()


async def test_remove_dir_tolerant_removes_plain_file(tmp_path: pathlib.Path) -> None:
    stray = tmp_path / "stray"
    stray.write_text("not a dir\n")

    await FileManager(logger=logger).remove_dir(stray, tolerant=True)

    assert not stray.exists()


async def test_remove_dir_tolerant_tolerates_absent_path(
    tmp_path: pathlib.Path,
) -> None:
    await FileManager(logger=logger).remove_dir(
        tmp_path / "never_existed", tolerant=True
    )


async def test_remove_dir_tolerant_does_not_follow_symlink_out_of_tree(
    tmp_path: pathlib.Path,
) -> None:
    """Removing a tree must delete a symlink inside it, never the linked-to
    content outside of it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("outside content\n")

    tree = _make_dir_tree(tmp_path / "tree")
    (tree / "link").symlink_to(outside / "target")

    await FileManager(logger=logger).remove_dir(tree, tolerant=True)

    assert not tree.exists()
    assert (outside / "target").exists()


async def test_reading_an_absent_path_raises_file_not_found(
    tmp_path: pathlib.Path,
) -> None:
    """Asking for the content or version of a file that is not there is an
    error, never an empty or zero answer — a caller that reads a stale path
    must hear about it rather than silently continue with plausible-looking
    nothing."""
    manager = FileManager(logger=logger)
    missing = tmp_path / "never_existed.py"

    with pytest.raises(ifilemanager.FileNotFound):
        await manager.get_content(missing)
    with pytest.raises(ifilemanager.FileNotFound):
        await manager.get_file_version(missing)
    assert await manager.file_exists(missing) is False


async def test_rename_file_onto_an_existing_target(
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "source.py"
    target = tmp_path / "target.py"
    source.write_text("new content\n")
    target.write_text("old content\n")
    manager = FileManager(logger=logger)

    with pytest.raises(ifilemanager.FileAlreadyExists):
        await manager.rename_file(source, target)
    assert source.exists()
    assert target.read_text() == "old content\n"

    await manager.rename_file(source, target, overwrite=True)
    assert not source.exists()
    assert target.read_text() == "new content\n"


async def test_rename_file_creates_the_target_parent_directory(
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("x = 1\n")

    await FileManager(logger=logger).rename_file(
        source, tmp_path / "new_dir" / "source.py"
    )

    assert not source.exists()
    assert (tmp_path / "new_dir" / "source.py").read_text() == "x = 1\n"


async def test_delete_file_missing_ok_gates_absence(
    tmp_path: pathlib.Path,
) -> None:
    manager = FileManager(logger=logger)
    missing = tmp_path / "never_existed.py"

    with pytest.raises(ifilemanager.FileNotFound):
        await manager.delete_file(missing)
    await manager.delete_file(missing, missing_ok=True)


async def test_delete_file_on_a_directory_raises(
    tmp_path: pathlib.Path,
) -> None:
    directory = tmp_path / "package"
    directory.mkdir()

    with pytest.raises(ifilemanager.DeleteFileError):
        await FileManager(logger=logger).delete_file(directory)

    assert directory.exists()


async def test_get_content_decodes_utf8_regardless_of_locale(
    tmp_path: pathlib.Path,
) -> None:
    """UTF-8 content must decode even where the default text encoding is the
    locale's, cp1252 on Windows."""
    path = tmp_path / "subject.py"
    path.write_bytes(_NON_ASCII.encode("utf-8"))

    assert await FileManager(logger=logger).get_content(path) == _NON_ASCII


async def test_save_file_encodes_utf8_regardless_of_locale(
    tmp_path: pathlib.Path,
) -> None:
    """Content must be written as UTF-8 even where the default text encoding
    is the locale's, cp1252 on Windows."""
    path = tmp_path / "subject.py"

    await FileManager(logger=logger).save_file(path, _NON_ASCII)

    assert path.read_bytes() == _NON_ASCII.encode("utf-8")
