import pathlib

import pytest

from finecode_extension_runner.services import _file_loc


class _SampleClassForFileLoc:
    pass


def test_file_loc_returns_relative_path_when_class_is_inside_project_dir() -> None:
    """_file_loc returns a path relative to project_dir when the class's source file lives under it."""
    project_dir = pathlib.Path(__file__).parent

    result = _file_loc(_SampleClassForFileLoc, project_dir)

    assert result is not None
    path_part, _, line_part = result.rpartition(":")
    assert path_part == "test_file_loc.py"
    assert line_part.isdigit()
    assert int(line_part) > 0


def test_file_loc_returns_absolute_path_when_class_is_outside_project_dir(
    tmp_path: pathlib.Path,
) -> None:
    """_file_loc falls back to an absolute path when the class's source file is not under project_dir."""
    result = _file_loc(_SampleClassForFileLoc, tmp_path)

    assert result is not None
    path_part, _, line_part = result.rpartition(":")
    assert pathlib.Path(path_part).is_absolute()
    assert line_part.isdigit()
    assert path_part != "test_file_loc.py"


def test_file_loc_returns_absolute_path_when_project_dir_is_none() -> None:
    """_file_loc always returns an absolute path when no project_dir is given, never a relative one."""
    result = _file_loc(_SampleClassForFileLoc, None)

    assert result is not None
    path_part, _, line_part = result.rpartition(":")
    assert pathlib.Path(path_part).is_absolute()
    assert line_part.isdigit()


def test_file_loc_returns_none_for_dynamically_created_class(
    tmp_path: pathlib.Path,
) -> None:
    """_file_loc returns None when a class has no retrievable source (e.g. built via type())."""
    dynamic_cls = type("X", (), {})

    result = _file_loc(dynamic_cls, tmp_path)

    assert result is None
