from __future__ import annotations

import pathlib
import sys

import pytest

from finecode.wm_server.runner import finecode_cmd

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="test builds a POSIX-style venv layout"
)


def _make_venv(venv_dir_path: pathlib.Path, *, recorded_path: pathlib.Path) -> None:
    bin_dir = venv_dir_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/usr/bin/env python3\n")
    (bin_dir / "activate").write_text(f"VIRTUAL_ENV='{recorded_path}'\nexport VIRTUAL_ENV\n")


def test_get_python_cmd_returns_path_when_venv_was_not_relocated(
    tmp_path: pathlib.Path,
) -> None:
    project_path = tmp_path / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev")
    _make_venv(venv_dir_path, recorded_path=venv_dir_path)

    result = finecode_cmd.get_python_cmd(project_path, "dev")

    assert result == (venv_dir_path / "bin" / "python").as_posix()


def test_get_python_cmd_raises_venv_relocated_error_when_recorded_path_differs(
    tmp_path: pathlib.Path,
) -> None:
    """Simulates a project directory that was moved/renamed after its venv was
    created: the activate script still records the old (now-nonexistent) path,
    even though ``bin/python`` itself exists and would otherwise look healthy.
    """
    project_path = tmp_path / "new_location" / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev")
    old_venv_dir_path = tmp_path / "old_location" / "myproject" / ".venvs" / "dev"
    _make_venv(venv_dir_path, recorded_path=old_venv_dir_path)

    with pytest.raises(finecode_cmd.VenvRelocatedError):
        finecode_cmd.get_python_cmd(project_path, "dev")


def test_get_python_cmd_raises_plain_value_error_when_venv_missing(
    tmp_path: pathlib.Path,
) -> None:
    project_path = tmp_path / "myproject"

    with pytest.raises(ValueError) as exc_info:
        finecode_cmd.get_python_cmd(project_path, "dev")

    assert not isinstance(exc_info.value, finecode_cmd.VenvRelocatedError)


def test_get_python_cmd_tolerates_missing_activate_script(
    tmp_path: pathlib.Path,
) -> None:
    """A venv without an activate script (unusual, but not our concern to enforce)
    should fall back to trusting ``bin/python`` rather than crashing."""
    project_path = tmp_path / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev")
    bin_dir = venv_dir_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/usr/bin/env python3\n")

    result = finecode_cmd.get_python_cmd(project_path, "dev")

    assert result == (venv_dir_path / "bin" / "python").as_posix()
