"""Tests for the project boundary a file listing must respect.

A file belongs to exactly one project.  When two projects both claim it, whichever
configuration reaches it first decides how it is linted, formatted or type-checked, and
restricting an operation to one project stops meaning anything.
"""

from __future__ import annotations

import pathlib

from finecode_extension_api.workspace_utils import (
    group_files_by_project,
    nested_project_dirs,
    walk_project_files,
)

_WS = pathlib.Path("/ws")


def test_only_a_project_inside_this_one_is_a_boundary() -> None:
    # a sibling project shares no files with this one, and every project is trivially
    # relative to itself -- treating that as nesting would empty every listing
    nested = nested_project_dirs(
        _WS, [_WS, _WS / "packages" / "inner", pathlib.Path("/other")]
    )

    assert nested == [_WS / "packages" / "inner"]


def test_a_directory_that_is_not_a_project_is_not_a_boundary() -> None:
    # nothing else lists these files: a directory without FineCode config gets no
    # runner, so treating it as a boundary would silently stop checking its files
    assert nested_project_dirs(_WS, [_WS]) == []


def test_files_of_a_nested_project_are_not_listed_for_the_outer_one(
    tmp_path: pathlib.Path,
) -> None:
    # restricting an operation to the outer project (lint --project-paths) must not
    # quietly process the inner one too, which is what a plain recursive walk delivers
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").touch()
    (tmp_path / "inner" / "src").mkdir(parents=True)
    (tmp_path / "inner" / "src" / "inner.py").touch()

    found = walk_project_files(
        tmp_path, suffix=".py", excluded_dirs=[tmp_path / "inner"]
    )

    assert found == [tmp_path / "src" / "app.py"]


def test_an_excluded_directory_is_never_descended_into(
    tmp_path: pathlib.Path,
) -> None:
    # the point of pruning rather than filtering afterwards: on a workspace root the
    # excluded subtrees (virtualenvs, nested projects) are where the walk's time goes
    (tmp_path / "inner").mkdir()
    visited_marker = tmp_path / "inner" / "deep"
    visited_marker.mkdir()
    (visited_marker / "x.py").touch()

    found = walk_project_files(
        tmp_path, suffix=".py", excluded_dirs=[tmp_path / "inner"]
    )

    assert found == []


def test_hidden_directories_are_not_source(tmp_path: pathlib.Path) -> None:
    # .venvs holds installed dependencies, not this project's code -- and walking one
    # costs more than the whole rest of the project
    (tmp_path / ".venvs" / "dev").mkdir(parents=True)
    (tmp_path / ".venvs" / "dev" / "vendored.py").touch()
    (tmp_path / "app.py").touch()

    found = walk_project_files(tmp_path, suffix=".py")

    assert found == [tmp_path / "app.py"]


def test_hidden_files_are_not_source(tmp_path: pathlib.Path) -> None:
    # a dotfile configures the tools rather than being checked by them: listing
    # .ruff.toml as a toml source artifact has `format` rewriting ruff's own config
    (tmp_path / ".ruff.toml").touch()
    (tmp_path / "pyproject.toml").touch()

    found = walk_project_files(tmp_path, suffix=".toml")

    assert found == [tmp_path / "pyproject.toml"]


def test_only_the_asked_for_suffix_comes_back(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "app.py").touch()

    assert walk_project_files(tmp_path, suffix=".toml") == [tmp_path / "pyproject.toml"]


def test_a_file_belongs_to_the_deepest_project_containing_it() -> None:
    # the pairing of the two halves: what a listing keeps, grouping must attribute to
    # the same project
    files = [_WS / "src" / "app.py", _WS / "packages" / "inner" / "src" / "inner.py"]

    grouped = group_files_by_project(files, [_WS, _WS / "packages" / "inner"])

    assert grouped == {
        _WS: [_WS / "src" / "app.py"],
        _WS / "packages" / "inner": [_WS / "packages" / "inner" / "src" / "inner.py"],
    }
