#!/usr/bin/env python3
"""Prints `-e <path>` args for every local monorepo package reachable from the root
project's `dev_workspace` dependency group, by following `[project].dependencies`
edges (e.g. `finecode` -> `finecode_extension_runner` -> ...). Used by
setup-dev-workspace.sh instead of a hand-maintained package list, which had already
gone stale (a removed package still listed, several added presets missing) — see
docs/guides/developing-finecode.md#continuous-integration.

Must be run with the repo root as the working directory. Uses only the standard
library (tomllib, Python 3.11+) since finecode is not installed yet at this point.
"""

import pathlib
import re
import sys
import tomllib

REPO_ROOT = pathlib.Path.cwd()

_SPEC_RE = re.compile(r"[\[<>=!~; ]")


def _bare_name(spec: str) -> str:
    return _SPEC_RE.split(spec, maxsplit=1)[0].strip()


def _package_dirs(repo_root: pathlib.Path) -> list[pathlib.Path]:
    dirs = [repo_root]
    # NOTE: `finecode_*` also matches `finecode_internal_experiments`, the
    # gitignored clone of the private repo. It is dropped today only by the
    # `pyproject.toml` existence check three lines below — that directory holds
    # no project of its own, just `presets/` and `extensions/` subtrees. Add a
    # `pyproject.toml` there and it silently becomes an editable install.
    dirs += sorted(repo_root.glob("finecode_*"))
    dirs += sorted(repo_root.glob("extensions/*"))
    dirs += sorted(repo_root.glob("presets/*"))
    # The private repo mirrors the public layout, so two extra globs reach it.
    # These match nothing on a clean public checkout, where the clone is absent.
    # Deliberately NOT an os.walk: `tests/__testdata__` holds 5 fixture
    # `pyproject.toml` files that the workspace scanner explicitly skips, and a
    # generalised walk would install them as editable packages unless it
    # replicated that exclusion and the `finecode_config_dump` one below.
    dirs += sorted(repo_root.glob("finecode_internal_experiments/presets/*"))
    dirs += sorted(repo_root.glob("finecode_internal_experiments/extensions/*"))
    return [
        d
        for d in dirs
        # `finecode_config_dump` is a generated dump_config artifact directory (a
        # sibling of nearly every package in this repo), not a real package — its
        # dumped pyproject.toml can even declare `[project] name = "finecode"` when
        # it holds the root project's own dump, which would otherwise collide with
        # the real root package.
        if d.name != "finecode_config_dump" and (d / "pyproject.toml").is_file()
    ]


def _dev_workspace_roots(pyproject: dict) -> list[str]:
    groups = pyproject.get("dependency-groups", {})
    group = groups.get("dev_workspace", []) if isinstance(groups, dict) else []
    if not isinstance(group, list):
        return []
    return [_bare_name(entry) for entry in group if isinstance(entry, str)]


def _user_config_dev_workspace_roots(repo_root: pathlib.Path) -> list[str]:
    """Roots declared in the gitignored `finecode-user.toml` beside the root
    pyproject, or `[]` when it is absent or unreadable.

    Absent-or-malformed is tolerated deliberately: this is a *personal* file, and
    one developer's typo must never break the shared bootstrap. A malformed file
    is reported on stderr rather than swallowed, because the alternative failure
    mode -- a preset silently vanishing from the venv -- is far harder to
    diagnose than a line of bootstrap noise.
    """
    user_config_path = repo_root / "finecode-user.toml"
    if not user_config_path.is_file():
        return []
    try:
        with user_config_path.open("rb") as f:
            user_config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(
            f"warning: ignoring {user_config_path.name}: {exc}",
            file=sys.stderr,
        )
        return []
    return _dev_workspace_roots(user_config)


def resolve_workspace_packages(
    repo_root: pathlib.Path, roots: list[str] | None = None
) -> dict[str, pathlib.Path]:
    """Return {package_name: package_dir} for every local monorepo package
    reachable from *roots* by following `[project].dependencies` edges.

    *roots* defaults to the root project's `dev_workspace` dependency group,
    **plus** the same group in the gitignored `finecode-user.toml` beside it
    when that file exists (the script's own CLI use case). The user file is
    where packages a clean public checkout cannot install are declared — they
    are still installed editable from source on a machine that has them, so
    the bootstrap must see them. Includes the root package itself when
    reachable. Callers that only need a specific package's own closure (e.g.
    a test that wants just `finecode` and what it pulls in, not every root
    package in the monorepo's dev_workspace group), or that deliberately want
    the *tracked* closure only and must therefore ignore the user layer, can
    pass an explicit `roots=["finecode"]`.
    """
    # name -> (path, dependency names)
    packages: dict[str, tuple[pathlib.Path, list[str]]] = {}
    for package_dir in _package_dirs(repo_root):
        with (package_dir / "pyproject.toml").open("rb") as f:
            pyproject = tomllib.load(f)
        name = pyproject.get("project", {}).get("name")
        if not name:
            continue
        deps = [_bare_name(d) for d in pyproject["project"].get("dependencies", [])]
        packages[name] = (package_dir, deps)

    if roots is None:
        with (repo_root / "pyproject.toml").open("rb") as f:
            root_pyproject = tomllib.load(f)
        roots = _dev_workspace_roots(root_pyproject)
        roots += _user_config_dev_workspace_roots(repo_root)

    visited: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        visited.add(name)
        if name not in packages:
            continue  # not a local package — installed from PyPI as declared
        _, deps = packages[name]
        queue.extend(dep for dep in deps if dep not in visited)

    return {name: packages[name][0] for name in visited if name in packages}


def main() -> None:
    packages = resolve_workspace_packages(REPO_ROOT)
    editable_dirs = sorted(path for path in packages.values() if path != REPO_ROOT)

    for package_dir in editable_dirs:
        rel = package_dir.relative_to(REPO_ROOT).as_posix()
        print(f"-e ./{rel}")


if __name__ == "__main__":
    sys.exit(main())
