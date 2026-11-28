"""The packages that use the peer-cancel API must not install an older library.

REQUIREMENT: ``finecode`` and ``finecode_extension_runner`` call
``finecode_jsonrpc`` features added in 0.1.1. If either still accepted 0.1.0, an
environment could resolve to a library that lacks them, and the failure would
show up as an unrelated import or signature error at runtime.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import tomllib
import typing

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIN_VERSION = Version("0.1.1")


def _load(project_dir: str) -> dict[str, typing.Any]:
    path = _REPO_ROOT / project_dir / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _jsonrpc_specifier(project_dir: str) -> SpecifierSet:
    dependencies = _load(project_dir)["project"]["dependencies"]
    specifiers = [
        requirement.specifier
        for requirement in map(Requirement, dependencies)
        if requirement.name == "finecode_jsonrpc"
    ]
    assert len(specifiers) == 1, f"{project_dir} must declare finecode_jsonrpc once"
    return specifiers[0]


def test_library_declares_the_new_version() -> None:
    assert Version(_load("finecode_jsonrpc")["project"]["version"]) == _MIN_VERSION


@pytest.mark.parametrize("project_dir", [".", "finecode_extension_runner"])
def test_dependents_require_at_least_the_new_version(project_dir: str) -> None:
    specifier = _jsonrpc_specifier(project_dir)

    assert _MIN_VERSION in specifier
    assert Version("0.1.0") not in specifier


def test_installed_library_satisfies_the_declared_bounds() -> None:
    installed = Version(importlib.metadata.version("finecode_jsonrpc"))

    for project_dir in (".", "finecode_extension_runner"):
        assert installed in _jsonrpc_specifier(project_dir)
