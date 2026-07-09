from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server.runner import finecode_cmd

# These tests exercise the win32 branch of `_recorded_venv_path` / `get_python_cmd`
# regardless of the host OS the suite runs on, by monkeypatching `sys.platform` as
# seen by `finecode_cmd` and building a Windows-style `Scripts/` venv layout.


def _make_windows_venv(
    venv_dir_path: pathlib.Path, *, activate_bat_content: str
) -> None:
    scripts_dir = venv_dir_path / "Scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "python.exe").write_text("")
    (scripts_dir / "activate.bat").write_text(activate_bat_content)


@pytest.fixture(autouse=True)
def _win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finecode_cmd.sys, "platform", "win32")


def test_plain_set_format_is_not_mistaken_for_relocation(
    tmp_path: pathlib.Path,
) -> None:
    """Regression test: a plain `set "VIRTUAL_ENV=<path>"` line quotes the whole
    assignment, not just the path. The old regex captured everything inside the
    quotes (including the literal `VIRTUAL_ENV=` prefix), so the recorded path
    could never equal the real venv path and every such venv looked "relocated"
    even when it was not.
    """
    project_path = tmp_path / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev_workspace")
    _make_windows_venv(
        venv_dir_path,
        activate_bat_content=f'set "VIRTUAL_ENV={venv_dir_path}"\n',
    )

    result = finecode_cmd.get_python_cmd(project_path, "dev_workspace")

    assert result == (venv_dir_path / "Scripts" / "python.exe").as_posix()


def test_plain_set_format_raises_when_path_actually_differs(
    tmp_path: pathlib.Path,
) -> None:
    project_path = tmp_path / "new_location" / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev_workspace")
    old_venv_dir_path = (
        tmp_path / "old_location" / "myproject" / ".venvs" / "dev_workspace"
    )
    _make_windows_venv(
        venv_dir_path,
        activate_bat_content=f'set "VIRTUAL_ENV={old_venv_dir_path}"\n',
    )

    with pytest.raises(finecode_cmd.VenvRelocatedError):
        finecode_cmd.get_python_cmd(project_path, "dev_workspace")


def test_for_loop_indirection_format_is_not_mistaken_for_relocation(
    tmp_path: pathlib.Path,
) -> None:
    """uv-generated activate.bat files route VIRTUAL_ENV through a
    `for %%i in ("...") do @set "VIRTUAL_ENV=%%~fi"` indirection instead of a
    plain `set`."""
    project_path = tmp_path / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev_workspace")
    _make_windows_venv(
        venv_dir_path,
        activate_bat_content=(
            f'for %%i in ("{venv_dir_path}") do @set "VIRTUAL_ENV=%%~fi"\n'
        ),
    )

    result = finecode_cmd.get_python_cmd(project_path, "dev_workspace")

    assert result == (venv_dir_path / "Scripts" / "python.exe").as_posix()


def test_for_loop_indirection_format_raises_when_path_actually_differs(
    tmp_path: pathlib.Path,
) -> None:
    project_path = tmp_path / "new_location" / "myproject"
    venv_dir_path = finecode_cmd.get_venv_dir_path(project_path, "dev_workspace")
    old_venv_dir_path = (
        tmp_path / "old_location" / "myproject" / ".venvs" / "dev_workspace"
    )
    _make_windows_venv(
        venv_dir_path,
        activate_bat_content=(
            f'for %%i in ("{old_venv_dir_path}") do @set "VIRTUAL_ENV=%%~fi"\n'
        ),
    )

    with pytest.raises(finecode_cmd.VenvRelocatedError):
        finecode_cmd.get_python_cmd(project_path, "dev_workspace")
