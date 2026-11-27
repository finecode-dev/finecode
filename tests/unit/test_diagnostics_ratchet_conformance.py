"""Tests that every umbrella handler judges its findings through the ratchet.

A contributor that emits findings without calling the ratchet leaves them all
new, so the gate fails on legacy findings no snapshot records. The duty sits
at each emit point because concurrent handlers cannot see each other — this
test pins it statically by resolving every handler registered on
``inspect_code``/``audit_code`` through its package root (never a
repository-wide glob, which would match stale venv and build copies) and
requiring the contributor constant and the ratchet call.
"""

from __future__ import annotations

import ast
import os
import pathlib
import tomllib

import pytest

_WORKSPACE_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EXTENSIONS = _WORKSPACE_ROOT / "extensions"
_DEV_COMMON_PRESET = (
    _WORKSPACE_ROOT
    / "finecode_dev_common_preset"
    / "src"
    / "finecode_dev_common_preset"
    / "preset.toml"
)
_UMBRELLAS = ("inspect_code", "audit_code")
_EXEMPT = {
    "fine_inspect_code.FileExistenceValidationHandler",
    "fine_audit_code.FileExistenceValidationHandler",
}
_EXPECTED_PUBLIC = {
    "fine_lint.LintInspectCodeBridgeHandler",
    "fine_type_check.TypeCheckInspectCodeBridgeHandler",
    "fine_check_imports.CheckImportsAuditCodeBridgeHandler",
    "fine_envs.check_toolchains_audit_code_bridge_handler.CheckToolchainsAuditCodeBridgeHandler",
}
_PRIVATE_PRESET = _EXTENSIONS / "fine_knowledge" / "fine_knowledge" / "preset.toml"
_PRIVATE_HANDLER = "fine_knowledge.AuditPresetDepsHandler"


def _preset_paths() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for pkg in sorted(os.listdir(_EXTENSIONS)):
        candidate = _EXTENSIONS / pkg / pkg / "preset.toml"
        if candidate.is_file():
            paths.append(candidate)
    if _DEV_COMMON_PRESET.is_file():
        paths.append(_DEV_COMMON_PRESET)
    return paths


def _umbrella_handlers(preset_path: pathlib.Path) -> list[str]:
    with open(preset_path, "rb") as handle:
        data = tomllib.load(handle)
    actions = data.get("tool", {}).get("finecode", {}).get("action", {})
    sources: list[str] = []
    for name in _UMBRELLAS:
        handlers = actions.get(name, {}).get("handlers", [])
        sources.extend(entry["source"] for entry in handlers)
    return sources


def _package_root(package: str) -> pathlib.Path:
    candidate = _EXTENSIONS / package / package
    if candidate.is_dir():
        return candidate
    candidate = _DEV_COMMON_PRESET.parent / package
    if candidate.is_dir():
        return candidate
    raise AssertionError(f"no package root for {package!r}")


def _resolve_module(source: str) -> tuple[pathlib.Path, str]:
    segments = source.split(".")
    if len(segments) == 2:
        package, class_name = segments
        root = _package_root(package)
        init = root / "__init__.py"
        tree = ast.parse(init.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith(package + ".")
            ):
                for alias in node.names:
                    if alias.asname == class_name or alias.name == class_name:
                        module_file = root / (node.module.split(".")[-1] + ".py")
                        return module_file, class_name
        raise AssertionError(f"cannot resolve {source!r} through {init}")
    root = _package_root(segments[0])
    module_file = pathlib.Path(root, *segments[1:-1]).with_suffix(".py")
    return module_file, segments[-1]


def _contributor_id(module_path: pathlib.Path, class_name: str) -> str:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == "RATCHET_CONTRIBUTOR"
                    and statement.value is not None
                ):
                    return str(ast.literal_eval(statement.value))
    raise AssertionError(f"{class_name} in {module_path} has no RATCHET_CONTRIBUTOR")


def _calls_apply(module_path: pathlib.Path) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "apply"
        and any(keyword.arg == "contributor" for keyword in node.keywords)
        for node in ast.walk(tree)
    )


def _check(source: str) -> str:
    module_path, class_name = _resolve_module(source)
    assert module_path.is_file(), f"{source!r} resolves to missing {module_path}"
    contributor = _contributor_id(module_path, class_name)
    assert _calls_apply(module_path), f"{source!r} never calls apply(contributor=...)"
    return contributor


def test_every_umbrella_handler_applies_the_ratchet() -> None:
    found = [
        source for preset in _preset_paths() for source in _umbrella_handlers(preset)
    ]
    assert set(found) >= _EXEMPT
    assert set(found) >= _EXPECTED_PUBLIC
    contributors = [_check(source) for source in sorted(set(found) - _EXEMPT)]
    assert len(set(contributors)) == len(contributors)


def test_private_audit_preset_deps_applies_the_ratchet() -> None:
    if not _PRIVATE_PRESET.is_file():
        pytest.skip("private repo extensions/fine_knowledge is not present")
    found = _umbrella_handlers(_PRIVATE_PRESET)
    assert _PRIVATE_HANDLER in found
    assert _check(_PRIVATE_HANDLER) == "audit_preset_deps"
