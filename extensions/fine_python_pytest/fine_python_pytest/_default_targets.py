"""Decide whether pytest, run with no positional path, could still collect.

The two pytest handlers pass `default_test_dirs` entries as positional paths
only when the entries exist. When none exists, pytest falls back to its own
discovery from the project directory; this module keeps that fallback unless a
conservative check proves it would find nothing, so the caller can skip
spawning pytest at all.
"""

from __future__ import annotations

import configparser
import fnmatch
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path

# pytest's default `python_files` plus its default `--doctest-glob`.
_DEFAULT_TEST_PATTERNS = ("test_*.py", "*_test.py", "conftest.py", "test*.txt")

# pytest's default `norecursedirs`, minus the regex `\d+` and `re` patterns.
_NORECURSED_DIR_NAMES = {
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
}


def resolve_default_targets(
    project_dir: Path, default_test_dirs: Sequence[str], addopts: Sequence[str]
) -> list[str] | None:
    """Positional pytest paths for a run whose payload names none.

    Returns the existing entries of *default_test_dirs*. If none exists, returns
    `[]` (pytest's own discovery, as before) unless nothing could be collected,
    in which case returns `None`: the caller skips pytest.
    """
    if not default_test_dirs:
        return []

    existing = [d for d in default_test_dirs if (project_dir / d).exists()]
    if existing:
        return existing

    if _pytest_may_collect(project_dir, addopts):
        return []
    return None


def _pytest_may_collect(project_dir: Path, addopts: Sequence[str]) -> bool:
    """Whether pytest launched from *project_dir* with no positional path could
    still find something to collect.

    Any sign counts as True: doctest flags and positional paths smuggled
    through *addopts*, a pytest config file (a parse error counts too — let
    pytest surface it), or a default-pattern file anywhere under
    *project_dir*.
    """
    for opt in addopts:
        if opt.startswith("--doctest"):
            # --doctest-modules / --doctest-glob collect files outside test_*.py
            return True
        if not opt.startswith("-") and (project_dir / opt).exists():
            # a positional path smuggled in through addopts
            return True

    if (project_dir / "pytest.ini").exists() or (project_dir / ".pytest.ini").exists():
        return True

    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            with pyproject.open("rb") as f:
                config = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            return True
        # covers both `[tool.pytest.ini_options]` and pytest 9's native table
        if "pytest" in config.get("tool", {}):
            return True

    for config_file, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        if not (project_dir / config_file).exists():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(project_dir / config_file)
        except configparser.Error:
            return True
        if parser.has_section(section):
            return True

    for root, dirnames, filenames in os.walk(project_dir):
        if any(
            fnmatch.fnmatchcase(name, pattern)
            for name in filenames
            for pattern in _DEFAULT_TEST_PATTERNS
        ):
            return True
        # prune in place so deeper pruned subtrees are never descended into
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in _NORECURSED_DIR_NAMES
            and not d.endswith(".egg")
            and not (Path(root) / d / "pyvenv.cfg").exists()
        ]

    return False
