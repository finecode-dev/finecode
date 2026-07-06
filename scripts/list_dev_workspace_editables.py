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
    dirs += sorted(repo_root.glob("finecode_*"))
    dirs += sorted(repo_root.glob("extensions/*"))
    dirs += sorted(repo_root.glob("presets/*"))
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


def resolve_workspace_packages(
    repo_root: pathlib.Path, roots: list[str] | None = None
) -> dict[str, pathlib.Path]:
    """Return {package_name: package_dir} for every local monorepo package
    reachable from *roots* by following `[project].dependencies` edges.

    *roots* defaults to the root project's `dev_workspace` dependency group
    (the script's own CLI use case). Includes the root package itself when
    reachable. Callers that only need a specific package's own closure (e.g.
    a test that wants just `finecode` and what it pulls in, not every root
    package in the monorepo's dev_workspace group) can pass an explicit
    `roots=["finecode"]`.
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
        dev_workspace_group = (
            root_pyproject.get("dependency-groups", {}).get("dev_workspace", [])
        )
        roots = [_bare_name(entry) for entry in dev_workspace_group if isinstance(entry, str)]

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
