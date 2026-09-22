import pathlib
import tomllib
from collections.abc import Iterable

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider
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
    (including any leading whitespace, e.g. ` @ file:///...`).
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


def direct_reference(path: pathlib.Path) -> str:
    """The PEP 508 direct-reference suffix (` @ file:///…`) for a local path.

    `as_uri()` gives the RFC 8089 form on every OS (`file:///D:/…` on Windows,
    where `file://` + `as_posix()` would put the drive in the URI authority)
    and raises ValueError on a relative path instead of producing a URI that
    installers resolve against their cwd.
    """
    return f" @ {path.as_uri()}"


def workspace_package_ref(
    name: str,
    package: iprojectinfoprovider.WorkspacePackage,
) -> tuple[str, bool]:
    """Return the ``(version_or_source, editable)`` reference for a workspace package.

    An editable package installs from its source directory. A non-editable
    package installs from its built wheel; a non-editable package with no wheel
    means the wheelhouse is stale or absent, which is reported rather than
    silently falling back to an editable install (P5/R4).

    Raises:
        ActionFailedException: a wheel was required but missing.
    """
    if package.editable:
        return direct_reference(package.dir), True
    if package.wheel is None:
        raise code_action.ActionFailedException(
            f"Workspace package '{name}' has no built wheel in wheel mode. "
            "Run `finecode prepare-envs` to build the wheelhouse, or add it to "
            "[workspace.workspace_packages_install].exclude to install it editable."
        )
    return direct_reference(package.wheel), False


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
    ws_workspace_packages: dict[str, iprojectinfoprovider.WorkspacePackage],
) -> list[dict]:
    """For each workspace package referenced by a dependency, read its
    pyproject.toml and add any of its dependencies that are also workspace
    packages, recursively.

    The package's *source* directory is read from the workspace map, while the
    emitted reference comes from ``workspace_package_ref`` — so in wheel mode a
    dependency's source is still read for its dependency edges even though the
    installed artifact is its wheel.

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
        dep for dep in entry_by_name.values() if dep["name"] in ws_workspace_packages
    ]

    while queue:
        dep = queue.pop()
        package_dir = ws_workspace_packages[dep["name"]].dir

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
            if pkg_name not in ws_workspace_packages:
                continue
            if pkg_name in seen:
                if set(pkg_extras) <= seen[pkg_name]:
                    continue
                seen[pkg_name] |= set(pkg_extras)
                entry = entry_by_name[pkg_name]
                entry["extras"] = merge_extras(entry.get("extras", []), pkg_extras)
                queue.append(entry)
            else:
                seen[pkg_name] = set(pkg_extras)
                version_or_source, editable = workspace_package_ref(
                    pkg_name, ws_workspace_packages[pkg_name]
                )
                new_dep: dict = make_dep(
                    name=pkg_name,
                    version_or_source=version_or_source,
                    editable=editable,
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
    package: iprojectinfoprovider.WorkspacePackage | None = None,
) -> list[dict]:
    """Return `dependencies` with an entry for the project under test.

    Per ADR-0046, an env with install_project = true gets the project being
    configured installed from its own directory (editable) or, when the
    workspace package is in wheel mode, from its built wheel. Any existing entry
    for the same distribution — e.g. a rule-3 named reference in the dependency
    group (ADR-0018) — is removed first: the injected install always wins, so
    the project is installed exactly once.
    """
    canonical_project_name = canonicalize_name(project_name)
    replaced_extras: list[str] = []
    result = []
    for dep in dependencies:
        if canonicalize_name(dep["name"]) == canonical_project_name:
            replaced_extras = merge_extras(replaced_extras, dep.get("extras", []))
        else:
            result.append(dep)
    if package is not None:
        version_or_source, editable = workspace_package_ref(project_name, package)
    else:
        version_or_source = direct_reference(project_dir_path)
        editable = True
    result.append(
        make_dep(
            name=project_name,
            version_or_source=version_or_source,
            editable=editable,
            extras=replaced_extras,
        )
    )
    return result


__all__ = [
    "collect_transitive_editable_deps",
    "direct_reference",
    "get_dependency_name",
    "make_dep",
    "merge_extras",
    "process_raw_deps",
    "resolve_install_project",
    "split_dep_spec",
    "workspace_package_ref",
]
