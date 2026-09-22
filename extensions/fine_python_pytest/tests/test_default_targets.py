"""`resolve_default_targets` keeps pytest's fallback discovery unless nothing
collectable exists in the project.

The pytest handlers skip spawning pytest when the payload names no target, no
`default_test_dirs` entry exists, and nothing pytest would collect is present.
A skip that is too eager hides a whole project's tests, so every way pytest can
still find tests without a positional path — config files, default-pattern
files, doctests, addopts — must keep it running.
"""

from __future__ import annotations

import pathlib

import pytest

from fine_python_pytest._default_targets import resolve_default_targets


def test_empty_config_defers_to_pytest(tmp_path: pathlib.Path) -> None:
    """`default_test_dirs=[]` is the escape hatch: pytest's own discovery runs
    no matter what the project looks like."""
    assert resolve_default_targets(tmp_path, [], []) == []


def test_existing_dir_is_passed_through(tmp_path: pathlib.Path) -> None:
    """An existing `default_test_dirs` entry is passed to pytest as before."""
    (tmp_path / "tests").mkdir()
    assert resolve_default_targets(tmp_path, ["tests"], []) == ["tests"]


def test_truly_empty_project_skips_pytest(tmp_path: pathlib.Path) -> None:
    """A project with nothing pytest could collect must not spawn pytest."""
    assert resolve_default_targets(tmp_path, ["tests"], []) is None


def test_stray_test_file_keeps_discovery(tmp_path: pathlib.Path) -> None:
    """A default-pattern file anywhere under the project keeps pytest running;
    the project may keep tests next to sources."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "test_x.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_suffix_pattern_test_file_keeps_discovery(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x_test.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_conftest_keeps_discovery(tmp_path: pathlib.Path) -> None:
    """`conftest.py` can add collectors, so its presence keeps pytest running."""
    (tmp_path / "conftest.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_doctest_glob_file_keeps_discovery(tmp_path: pathlib.Path) -> None:
    """`test*.txt` matches pytest's default `--doctest-glob`."""
    (tmp_path / "test_notes.txt").write_text(":: doctest::\n")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_hidden_dir_is_pruned(tmp_path: pathlib.Path) -> None:
    """Dot-directories (`.venvs`, `.git`) hold nothing pytest would collect by
    default, so a test file there must not keep the spawn."""
    (tmp_path / ".venvs" / "lib").mkdir(parents=True)
    (tmp_path / ".venvs" / "lib" / "test_x.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) is None


@pytest.mark.parametrize("name", ["node_modules", "build", "dist"])
def test_norecursedirs_are_pruned(tmp_path: pathlib.Path, name: str) -> None:
    """pytest's default `norecursedirs` are skipped by pytest itself, so they
    must not keep the spawn either."""
    (tmp_path / name).mkdir()
    (tmp_path / name / "test_x.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) is None


def test_virtualenv_is_pruned(tmp_path: pathlib.Path) -> None:
    """pytest skips virtualenvs by default; a test file inside one is not
    evidence the project has tests."""
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (tmp_path / "env" / "lib").mkdir()
    (tmp_path / "env" / "lib" / "test_x.py").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) is None


def test_pytest_ini_keeps_discovery(tmp_path: pathlib.Path) -> None:
    """`pytest.ini` may set `testpaths` or `python_files` — only pytest can
    say what it would collect."""
    (tmp_path / "pytest.ini").write_text("")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_pyproject_pytest_table_keeps_discovery(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_pyproject_without_pytest_table_skips(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert resolve_default_targets(tmp_path, ["tests"], []) is None


def test_malformed_pyproject_keeps_discovery(tmp_path: pathlib.Path) -> None:
    """An unparseable config keeps the spawn: pytest's error output explains
    what is wrong; guessing would silence it."""
    (tmp_path / "pyproject.toml").write_text("[[[\n")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_setup_cfg_pytest_section_keeps_discovery(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\n")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_tox_ini_pytest_section_keeps_discovery(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "tox.ini").write_text("[pytest]\n")
    assert resolve_default_targets(tmp_path, ["tests"], []) == []


def test_doctest_addopts_keep_discovery(tmp_path: pathlib.Path) -> None:
    """`--doctest-modules`/`--doctest-glob` collect files outside `test_*.py`."""
    assert resolve_default_targets(tmp_path, ["tests"], ["--doctest-modules"]) == []


def test_unrelated_addopts_do_not_keep_discovery(
    tmp_path: pathlib.Path,
) -> None:
    """A non-collecting addopt (e.g. `--asyncio-mode=auto`) must not by itself
    keep pytest running."""
    assert resolve_default_targets(tmp_path, ["tests"], ["--asyncio-mode=auto"]) is None


def test_positional_addopts_path_keeps_discovery(
    tmp_path: pathlib.Path,
) -> None:
    """A positional path smuggled through `addopts` collects whatever it names."""
    (tmp_path / "checks").mkdir()
    assert resolve_default_targets(tmp_path, ["tests"], ["checks"]) == []
