from __future__ import annotations

import pathlib
import stat

import pytest
from loguru import logger

from finecode_extension_api.interfaces import ifilemanager
from finecode_extension_runner.impls.file_manager import FileManager


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


async def test_remove_dir_tolerant_tolerates_absent_path(tmp_path: pathlib.Path) -> None:
    await FileManager(logger=logger).remove_dir(tmp_path / "never_existed", tolerant=True)


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
