from __future__ import annotations

import pathlib

from fine_dep_graph._legacy_package_kind import (
    LegacyPackageKind,
    infer_legacy_package_kind,
    normalize_package_name,
)

_MANIFEST = [
    "fine_agent",
    "fine_audit_code",
    "fine_check_imports",
    "fine_code_hierarchy",
    "fine_dep_graph",
    "fine_dist_artifacts",
    "fine_docs",
    "fine_docs_markdown",
    "fine_envs",
    "fine_format",
    "fine_git",
    "fine_git_hooks",
    "fine_inlay_hints",
    "fine_inspect_code",
    "fine_lint",
    "fine_logs",
    "fine_python_check_imports",
    "fine_python_code_hierarchy",
    "fine_python_envs",
    "fine_python_format",
    "fine_python_inlay_hints",
    "fine_python_lint",
    "fine_python_recommended",
    "fine_python_semantic_tokens",
    "fine_python_src_artifacts",
    "fine_python_symbol_info",
    "fine_python_test",
    "fine_python_type_check",
    "fine_release",
    "fine_semantic_tokens",
    "fine_src_artifacts",
    "fine_symbol_info",
    "fine_system_setup",
    "fine_test",
    "fine_toml_recommended",
    "fine_type_check",
    "fine_wal_events",
]


def test_normalize_matches_seeder_expression() -> None:
    assert (
        normalize_package_name("Fine_Python.Format-Name") == "fine-python-format-name"
    )


def test_full_historical_set_is_preset_in_both_locations() -> None:
    assert len(_MANIFEST) == 37
    for name in _MANIFEST:
        assert (
            infer_legacy_package_kind(name, pathlib.Path("presets") / name)
            == LegacyPackageKind.PRESET
        )
        assert (
            infer_legacy_package_kind(name, pathlib.Path("extensions") / name)
            == LegacyPackageKind.PRESET
        )


def test_normalized_spellings_stay_preset() -> None:
    for spelling in ["Fine_Format", "fine.format", "FINE-FORMAT"]:
        assert (
            infer_legacy_package_kind(spelling, pathlib.Path("extensions/fine_format"))
            == LegacyPackageKind.PRESET
        )


def test_root_package_is_core() -> None:
    assert (
        infer_legacy_package_kind("finecode", pathlib.Path("finecode"))
        == LegacyPackageKind.CORE
    )
    assert (
        infer_legacy_package_kind(
            "finecode_dev_common_preset", pathlib.Path("finecode_dev_common_preset")
        )
        == LegacyPackageKind.CORE
    )


def test_retained_private_preset_path_stays_preset() -> None:
    assert (
        infer_legacy_package_kind(
            "fine_knowledge", pathlib.Path("presets/fine_knowledge")
        )
        == LegacyPackageKind.PRESET
    )


def test_unlisted_extension_stays_extension() -> None:
    assert (
        infer_legacy_package_kind(
            "fine_python_ruff", pathlib.Path("extensions/fine_python_ruff")
        )
        == LegacyPackageKind.EXTENSION
    )


def test_extensions_precede_presets_in_path() -> None:
    assert (
        infer_legacy_package_kind(
            "some_pkg", pathlib.Path("extensions/foo/presets/bar")
        )
        == LegacyPackageKind.EXTENSION
    )


def test_path_matching_is_case_insensitive() -> None:
    assert (
        infer_legacy_package_kind("some_pkg", pathlib.Path("Extensions/foo"))
        == LegacyPackageKind.EXTENSION
    )
    assert (
        infer_legacy_package_kind("some_pkg", pathlib.Path("Presets/foo"))
        == LegacyPackageKind.PRESET
    )
