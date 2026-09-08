"""Guards the placement rule that keeps the public CI pipeline installable.

**Tracked configuration in the public repo may only name packages a clean public
checkout can install: packages tracked in this repository, or packages published
to PyPI and listed in `.github/ci/allowed_external_packages.txt`.**

Everything else — packages held in private repositories and never published — is
declared in the gitignored `finecode-user.toml` layer, which a clean clone simply
does not have. That is why both tests below derive what they inspect from *git*
rather than from the filesystem: a name resolving to a directory that happens to
exist on one developer's disk is exactly the failure mode being guarded against,
and a guard that consulted the disk would pass or fail depending on whose machine
ran it.

Both tests skip when `.git` is absent (sdist installs, vendored checkouts), where
"is this tracked?" has no answer.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import tomllib
from typing import Any

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALLOWLIST_PATH = _REPO_ROOT / ".github" / "ci" / "allowed_external_packages.txt"

# Names outside these prefixes are third-party packages on PyPI (click, loguru,
# pytest...). Only FineCode's own namespaces can plausibly be an untracked local
# package, so only they are in scope.
_FIRST_PARTY_PREFIXES = ("fine_", "finecode_")
_FIRST_PARTY_EXACT = frozenset({"finecode"})

# Directories the workspace scanner itself skips when discovering projects
# (`read_configs.py`), reproduced here for the same reasons:
#   * `finecode_config_dump` holds generated `dump_config` output — a dumped
#     pyproject can even declare `[project] name = "finecode"`, colliding with
#     the real root package.
#   * `__testdata__` holds fixture projects whose config is deliberately
#     synthetic and is never installed.
_SKIP_PATH_PARTS = frozenset({"finecode_config_dump", "__testdata__"})

_SPEC_RE = re.compile(r"[\[<>=!~; ]")

pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".git").exists(),
    reason="needs a git checkout to tell tracked files from local-only ones",
)


def _bare_name(spec: str) -> str:
    """`fine_envs[audit]~=0.1.0a0` -> `fine_envs`."""
    return _SPEC_RE.split(spec, maxsplit=1)[0].strip()


def _is_first_party(name: str) -> bool:
    return name in _FIRST_PARTY_EXACT or name.startswith(_FIRST_PARTY_PREFIXES)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked_files() -> list[str]:
    return [line for line in _git("ls-files", "-z").split("\0") if line]


def _in_scope(rel_path: str) -> bool:
    return not _SKIP_PATH_PARTS.intersection(pathlib.PurePosixPath(rel_path).parts)


def _load_resolve_workspace_packages():
    """The bootstrap script's dependency-graph walk, loaded by path.

    It is a `scripts/` module rather than an importable package (it must run
    before any venv exists), so it is loaded the same way
    `tests/e2e/bootstrap/test_bootstrap.py` loads it.
    """
    script_path = _REPO_ROOT / "scripts" / "list_dev_workspace_editables.py"
    spec = importlib.util.spec_from_file_location(
        "list_dev_workspace_editables", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_workspace_packages


def _allowed_external_packages() -> set[str]:
    lines = _ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
    return {
        stripped
        for line in lines
        if (stripped := line.split("#", maxsplit=1)[0].strip())
    }


def _tracked_package_names() -> dict[str, str]:
    """`{[project].name: relative pyproject path}` for every tracked package."""
    names: dict[str, str] = {}
    for rel_path in _tracked_files():
        if not rel_path.endswith("pyproject.toml") or not _in_scope(rel_path):
            continue
        with (_REPO_ROOT / rel_path).open("rb") as f:
            pyproject = tomllib.load(f)
        name = pyproject.get("project", {}).get("name")
        if isinstance(name, str):
            names[name] = rel_path
    return names


def _collect_named_packages(
    config: Any, out: set[str], *, at_finecode_root: bool = False
) -> None:
    """Every package name a `[tool.finecode]` subtree names, at any depth.

    Handler and service declarations nest under several shapes — `[action.x]`
    with an inline `handlers = [...]` list, repeated `[[action.x.handlers]]`
    tables, `[[action_handler]]`, `[[service]]` — and new shapes keep appearing.
    Walking for the `dependencies` and `presets` keys wherever they occur is
    what keeps this guard from quietly stopping to cover a shape it was not
    written against.

    Two deliberate exemptions, both path-aware:

    - An `extra` table reached directly from the `tool.finecode` root is the
      gate mechanism; the names under it are inert until a workspace selects
      the extra in the gitignored finecode-workspace-user.toml, so they may
      name private packages. An `extra` key anywhere else (e.g. under
      `action.<x>`) is walked normally.
    - `[project.optional-dependencies]` is outside this walk entirely for the
      same reason: a name there is inert until an extra selects it. That shape
      is deliberately uncovered, not accidentally dropped.
    """
    if isinstance(config, dict):
        for key, value in config.items():
            if at_finecode_root and key == "extra":
                continue
            if key == "dependencies" and isinstance(value, list):
                out.update(_bare_name(v) for v in value if isinstance(v, str))
            elif key == "presets" and isinstance(value, list):
                out.update(
                    entry["source"]
                    for entry in value
                    if isinstance(entry, dict) and isinstance(entry.get("source"), str)
                )
            else:
                _collect_named_packages(value, out)
    elif isinstance(config, list):
        for item in config:
            _collect_named_packages(item, out)


def test_collect_named_packages_exempts_root_extra_gate() -> None:
    """The gate table at `tool.finecode.extra` is deliberately outside the
    guard's reach: a name there is inert until a workspace selects it."""
    names: set[str] = set()
    _collect_named_packages(
        {"extra": {"lint_fix": {"presets": [{"source": "fine_lint_fix"}]}}},
        names,
        at_finecode_root=True,
    )
    assert names == set()


def test_collect_named_packages_still_collects_nested_extra() -> None:
    """An `extra` key anywhere but the `tool.finecode` root is not the gate
    mechanism, so its `presets` must still be collected."""
    names: set[str] = set()
    _collect_named_packages(
        {"action": {"x": {"extra": {"presets": [{"source": "fine_lint_fix"}]}}}},
        names,
        at_finecode_root=True,
    )
    assert names == {"fine_lint_fix"}


def test_collect_named_packages_still_collects_root_presets() -> None:
    """The same name in an ordinary root `presets` list is not exempt."""
    names: set[str] = set()
    _collect_named_packages(
        {"presets": [{"source": "fine_lint_fix"}]}, names, at_finecode_root=True
    )
    assert names == {"fine_lint_fix"}


def _names_by_tracked_config_file() -> dict[str, set[str]]:
    """`{relative config path: first-party package names it declares}`."""
    result: dict[str, set[str]] = {}
    for rel_path in _tracked_files():
        basename = rel_path.rsplit("/", maxsplit=1)[-1]
        if basename not in ("pyproject.toml", "preset.toml") or not _in_scope(rel_path):
            continue
        with (_REPO_ROOT / rel_path).open("rb") as f:
            config = tomllib.load(f)
        names: set[str] = set()
        for dep in config.get("project", {}).get("dependencies", []):
            if isinstance(dep, str):
                names.add(_bare_name(dep))
        _collect_named_packages(
            config.get("tool", {}).get("finecode", {}), names, at_finecode_root=True
        )
        first_party = {name for name in names if _is_first_party(name)}
        if first_party:
            result[rel_path] = first_party
    return result


def test_dev_workspace_closure_is_fully_tracked() -> None:
    """Every local package the bootstrap installs editable is present in a clean clone, so `setup-dev-workspace.sh` reaches an installable set on a machine that has none of the private repositories.

    Roots come from the root `pyproject.toml`'s `dev_workspace` group *alone*,
    deliberately bypassing the `roots is None` default, which also reads the
    gitignored `finecode-user.toml`. Reading the user layer here would make the
    test fail on every developer machine that has the private packages — a
    working-copy property masquerading as a repository property.
    """
    resolve_workspace_packages = _load_resolve_workspace_packages()

    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        root_pyproject = tomllib.load(f)
    tracked_roots = [
        _bare_name(entry)
        for entry in root_pyproject["dependency-groups"]["dev_workspace"]
        if isinstance(entry, str)
    ]

    packages = resolve_workspace_packages(_REPO_ROOT, roots=tracked_roots)
    assert packages, "expected the dev_workspace closure to resolve some packages"

    # Allowlisted names are the same producer the second test handles: genuinely
    # external packages that some machines also happen to have checked out
    # locally, where `_package_dirs` then mistakes them for monorepo packages. On
    # a clean checkout they are not directories at all and never reach this
    # closure, so exempting them is what makes the assertion a property of the
    # repository rather than of whoever ran it.
    allowed_external = _allowed_external_packages()
    untracked = sorted(
        name
        for name, path in packages.items()
        if name not in allowed_external
        and not _git("ls-files", str(path.relative_to(_REPO_ROOT))).strip()
    )
    assert not untracked, (
        "these packages are installed editable by the bootstrap but have no tracked "
        f"files, so a clean public checkout cannot install them: {untracked}. "
        "Declare them in the gitignored finecode-user.toml layer instead of the "
        "root pyproject.toml."
    )


def test_tracked_configs_name_only_reachable_packages() -> None:
    """No tracked `pyproject.toml` or `preset.toml` names a first-party package a clean public checkout can neither find in the repository nor install from PyPI, so the public CI pipeline never depends on a package only some machines have.

    Covers `[project].dependencies`, `[tool.finecode].presets[].source` and
    handler/service `dependencies`. Handler `dependencies` are in scope even
    though they fail later than a bootstrap dependency would (at `prepare-envs`,
    not at install): later and less visibly is a reason to guard them, not a
    reason to exempt them.
    """
    tracked_packages = _tracked_package_names()
    allowed_external = _allowed_external_packages()

    violations: dict[str, list[str]] = {}
    for rel_path, names in sorted(_names_by_tracked_config_file().items()):
        unreachable = sorted(
            name
            for name in names
            if name not in tracked_packages and name not in allowed_external
        )
        if unreachable:
            violations[rel_path] = unreachable

    assert not violations, (
        "tracked configuration names packages a clean public checkout cannot "
        f"install: {violations}. Either the package belongs in this repository, "
        f"or it is published to PyPI and belongs in {_ALLOWLIST_PATH.name}, or it "
        "is private and must be declared in the gitignored finecode-user.toml "
        "layer instead of in tracked config."
    )
