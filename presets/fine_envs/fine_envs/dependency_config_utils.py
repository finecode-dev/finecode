import pathlib
import tomllib
from collections.abc import Iterable

from packaging.utils import canonicalize_name


def make_dep(
    name: str,
    version_or_source: str,
    editable: bool = False,
    extras: Iterable[str] = (),
) -> dict:
    """Build a dependency dict.

    The single constructor for dep dicts: every dep dict carries an `extras`
    key from birth, so no consumer needs to defend against its absence.
    """
    return {
        "name": name,
        "version_or_source": version_or_source,
        "editable": editable,
        "extras": sorted(set(extras)),
    }


def merge_extras(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    """Return the sorted union of two extras collections."""
    return sorted(set(existing) | set(new))


def get_dependency_name(dependency_str: str) -> str:
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]
    return dependency_str


def split_dep_spec(dependency_str: str) -> tuple[str, list[str], str]:
    """Split a dependency spec into its name, extras and the remainder.

    `pkg[a,b]~=1.0` -> (`pkg`, [`a`, `b`], `~=1.0`). A spec without extras
    yields an empty extras list and the whole remainder as the third element
    (including any leading whitespace, e.g. ` @ file://...`).
    """
    name = get_dependency_name(dependency_str)
    rest = dependency_str[len(name) :]
    extras: list[str] = []
    version_or_source = rest
    if rest.startswith("["):
        closing = rest.index("]")
        extras = sorted(
            {extra.strip() for extra in rest[1:closing].split(",") if extra.strip()}
        )
        version_or_source = rest[closing + 1 :]
    return name, extras, version_or_source


def process_raw_deps(
    raw_deps: list,
    dependencies: list,
    deps_groups: dict,
    project_def_path: pathlib.Path,
    _seen: dict[str, int] | None = None,
) -> None:
    if _seen is None:
        _seen = {}
    for raw_dep in raw_deps:
        if isinstance(raw_dep, str):
            name, extras, version_or_source = split_dep_spec(raw_dep)
            if name in _seen:
                existing = dependencies[_seen[name]]
                existing["extras"] = merge_extras(existing["extras"], extras)
                continue
            _seen[name] = len(dependencies)
            dependencies.append(
                make_dep(
                    name=name,
                    version_or_source=version_or_source,
                    editable=False,
                    extras=extras,
                )
            )
        elif isinstance(raw_dep, dict) and "include-group" in raw_dep:
            included_group_deps = deps_groups.get(raw_dep["include-group"], [])
            process_raw_deps(
                included_group_deps,
                dependencies,
                deps_groups,
                project_def_path,
                _seen,
            )


def collect_transitive_editable_deps(
    dependencies: list[dict],
    ws_editable_packages: dict[str, pathlib.Path],
) -> list[dict]:
    """For each editable dep, read its pyproject.toml and add any of its dependencies
    that are also workspace editable packages, recursively.

    Extras are a set-valued attribute of a node: a second reference to a node
    already seen merges its extras into the existing entry, and the node is
    re-walked only when its extras set grows. This keeps one entry per
    distribution while still discovering dependencies reachable only through an
    extra.
    """
    entry_by_name: dict[str, dict] = {}
    seen: dict[str, set[str]] = {}
    for dep in dependencies:
        name = dep["name"]
        if name in entry_by_name:
            entry_by_name[name]["extras"] = merge_extras(
                entry_by_name[name].get("extras", []), dep.get("extras", [])
            )
            seen[name] = set(entry_by_name[name]["extras"])
        else:
            entry_by_name[name] = dep
            seen[name] = set(dep.get("extras", []))

    result: list[dict] = []
    queue = [
        dep
        for dep in entry_by_name.values()
        if dep.get("editable") and " @ file://" in dep.get("version_or_source", "")
    ]

    while queue:
        dep = queue.pop()
        _, _, path_str = dep["version_or_source"].partition("file://")
        package_dir = pathlib.Path(path_str)

        pyproject_path = package_dir / "pyproject.toml"
        if not pyproject_path.exists():
            continue

        with open(pyproject_path, "rb") as f:
            config = tomllib.load(f)

        dep_names: dict[str, set[str]] = {}
        for dep_str in config.get("project", {}).get("dependencies", []):
            if isinstance(dep_str, str):
                name, extras, _ = split_dep_spec(dep_str)
                dep_names[name] = merge_extras(dep_names.get(name, []), extras)
        for group_deps in config.get("dependency-groups", {}).values():
            for group_dep in group_deps:
                if isinstance(group_dep, str):
                    name, extras, _ = split_dep_spec(group_dep)
                    dep_names[name] = merge_extras(dep_names.get(name, []), extras)
        optional_deps = config.get("project", {}).get("optional-dependencies", {})
        for extra in dep.get("extras", []):
            for extra_dep in optional_deps.get(extra, []):
                if isinstance(extra_dep, str):
                    name, extras, _ = split_dep_spec(extra_dep)
                    dep_names[name] = merge_extras(dep_names.get(name, []), extras)

        for pkg_name, pkg_extras in dep_names.items():
            if pkg_name not in ws_editable_packages:
                continue
            resolved = ws_editable_packages[pkg_name]
            if pkg_name in seen:
                if set(pkg_extras) <= seen[pkg_name]:
                    continue
                seen[pkg_name] |= set(pkg_extras)
                entry = entry_by_name[pkg_name]
                entry["extras"] = merge_extras(entry.get("extras", []), pkg_extras)
                if entry.get("editable") and " @ file://" in entry.get(
                    "version_or_source", ""
                ):
                    queue.append(entry)
            else:
                seen[pkg_name] = set(pkg_extras)
                new_dep: dict = make_dep(
                    name=pkg_name,
                    version_or_source=f" @ file://{resolved.as_posix()}",
                    editable=True,
                    extras=pkg_extras,
                )
                entry_by_name[pkg_name] = new_dep
                result.append(new_dep)
                queue.append(new_dep)

    return result


def resolve_install_project(
    dependencies: list[dict],
    project_name: str,
    project_dir_path: pathlib.Path,
) -> list[dict]:
    """Return `dependencies` with an editable entry for the project under test.

    Per ADR-0046, an env with install_project = true gets the project being
    configured installed editable from its own directory. Any existing entry
    for the same distribution — e.g. a rule-3 named reference in the
    dependency group (ADR-0018) — is removed first: the editable install
    always wins, so the project is installed exactly once.
    """
    canonical_project_name = canonicalize_name(project_name)
    replaced_extras: list[str] = []
    result = []
    for dep in dependencies:
        if canonicalize_name(dep["name"]) == canonical_project_name:
            replaced_extras = merge_extras(replaced_extras, dep.get("extras", []))
        else:
            result.append(dep)
    result.append(
        make_dep(
            name=project_name,
            version_or_source=f" @ file://{project_dir_path.as_posix()}",
            editable=True,
            extras=replaced_extras,
        )
    )
    return result


__all__ = [
    "collect_transitive_editable_deps",
    "get_dependency_name",
    "make_dep",
    "merge_extras",
    "process_raw_deps",
    "resolve_install_project",
    "split_dep_spec",
]
