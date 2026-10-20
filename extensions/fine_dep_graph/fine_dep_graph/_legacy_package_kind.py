from __future__ import annotations

import enum
import pathlib
import re

__all__ = ["LegacyPackageKind", "infer_legacy_package_kind", "normalize_package_name"]


class LegacyPackageKind(enum.StrEnum):
    CORE = "core"
    EXTENSION = "extension"
    PRESET = "preset"


_NORMALIZE_RE = re.compile(r"[-_.]+")


_LEGACY_PRESET_NAMES: frozenset[str] = frozenset(
    {
        "fine-agent",
        "fine-audit-code",
        "fine-check-imports",
        "fine-code-hierarchy",
        "fine-dep-graph",
        "fine-dist-artifacts",
        "fine-docs",
        "fine-docs-markdown",
        "fine-envs",
        "fine-format",
        "fine-git",
        "fine-git-hooks",
        "fine-inlay-hints",
        "fine-inspect-code",
        "fine-lint",
        "fine-logs",
        "fine-python-check-imports",
        "fine-python-code-hierarchy",
        "fine-python-envs",
        "fine-python-format",
        "fine-python-inlay-hints",
        "fine-python-lint",
        "fine-python-recommended",
        "fine-python-semantic-tokens",
        "fine-python-src-artifacts",
        "fine-python-symbol-info",
        "fine-python-test",
        "fine-python-type-check",
        "fine-release",
        "fine-semantic-tokens",
        "fine-src-artifacts",
        "fine-symbol-info",
        "fine-system-setup",
        "fine-test",
        "fine-toml-recommended",
        "fine-type-check",
        "fine-wal-events",
    }
)


def normalize_package_name(name: str) -> str:
    """PEP 503 canonical name: lowercase with runs of [-_.] replaced by a single dash."""
    return _NORMALIZE_RE.sub("-", name).lower()


def infer_legacy_package_kind(
    package_name: str, project_path: pathlib.Path
) -> LegacyPackageKind:
    """Historical graph label for *package_name* at *project_path*.

    The manifest set freezes the pre-consolidation preset locations; path
    inference below preserves the former directory-order rule for all other
    packages.
    """
    if normalize_package_name(package_name) in _LEGACY_PRESET_NAMES:
        return LegacyPackageKind.PRESET
    lower_parts = [p.lower() for p in project_path.parts]
    if "extensions" in lower_parts:
        return LegacyPackageKind.EXTENSION
    if "presets" in lower_parts:
        return LegacyPackageKind.PRESET
    return LegacyPackageKind.CORE
