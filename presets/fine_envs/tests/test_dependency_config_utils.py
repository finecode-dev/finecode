import pathlib

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectinfoprovider

from fine_envs.dependency_config_utils import (
    collect_transitive_editable_deps,
    make_dep,
    resolve_install_project,
    workspace_package_ref,
)


def _pkg(
    dir_path: pathlib.Path,
    wheel: pathlib.Path | None = None,
    *,
    editable: bool = True,
) -> iprojectinfoprovider.WorkspacePackage:
    return iprojectinfoprovider.WorkspacePackage(
        dir=dir_path, wheel=wheel, editable=editable
    )


def _make_pkg(dir_path: pathlib.Path, name: str, content: str) -> pathlib.Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n' + content)
    return dir_path


def test_resolve_install_project_adds_editable_entry_for_project_dir(
    tmp_path: pathlib.Path,
) -> None:
    """install_project adds an editable requirement for the project's own directory.

    This is how a test-runner preset makes the project under test importable in
    its handler's env without ever knowing the consumer's package name (ADR-0046).
    """
    result = resolve_install_project([], "my_project", tmp_path)

    assert len(result) == 1
    entry = result[0]
    assert entry["name"] == "my_project"
    assert entry["editable"] is True
    assert entry["version_or_source"] == f" @ file://{tmp_path.as_posix()}"


def test_resolve_install_project_replaces_named_requirement_for_same_distribution(
    tmp_path: pathlib.Path,
) -> None:
    """A rule-3 named requirement for the project is replaced, not duplicated.

    An env can both opt into install_project and name the project in its
    dependency group; the project must still be installed exactly once,
    editable — otherwise pip/uv would see two conflicting requirements for the
    same distribution.
    """
    dependencies = [
        {"name": "my_project", "version_or_source": ">=1.0", "editable": False},
        {"name": "other_dep", "version_or_source": "", "editable": False},
    ]

    result = resolve_install_project(dependencies, "my_project", tmp_path)

    assert len(result) == 2
    names = {dep["name"] for dep in result}
    assert names == {"my_project", "other_dep"}
    my_project_entry = next(dep for dep in result if dep["name"] == "my_project")
    assert my_project_entry["editable"] is True
    assert my_project_entry["version_or_source"] == f" @ file://{tmp_path.as_posix()}"


def test_resolve_install_project_matches_by_canonical_name(
    tmp_path: pathlib.Path,
) -> None:
    """The dedup match is canonical-name-based, not exact-string-based.

    PEP 503 treats `My-Project`, `my_project`, and `my.project` as the same
    distribution; a byte-for-byte comparison would leave a stale, non-editable
    requirement installed alongside the editable one.
    """
    dependencies = [
        {"name": "My_Project", "version_or_source": ">=1.0", "editable": False},
    ]

    result = resolve_install_project(dependencies, "my-project", tmp_path)

    assert len(result) == 1
    assert result[0]["editable"] is True


def test_install_project_entry_is_included_in_transitive_editable_walk(
    tmp_path: pathlib.Path,
) -> None:
    """The project's own workspace-editable dependencies are discovered too.

    Regression test: install_env_install_deps_handler calls
    resolve_install_project() before collect_transitive_editable_deps(), not
    after. If the project's injected editable entry is added only after the
    transitive walk has already run, a project whose only edge into the
    dependency graph is that injected entry has its own transitive
    workspace-editable dependencies silently dropped — the installer then
    fails with "package not found in registry" for what is actually a local
    workspace package (ADR-0046). Calling the two functions in this order is
    what keeps that from happening.
    """
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "my_project"\ndependencies = ["my_project_dep>=0.1.0"]\n'
    )
    dep_dir = tmp_path / "my_project_dep"
    dep_dir.mkdir()
    ws_workspace_packages = {
        "my_project": _pkg(project_dir),
        "my_project_dep": _pkg(dep_dir),
    }

    dependencies = resolve_install_project([], "my_project", project_dir)
    dependencies.extend(
        collect_transitive_editable_deps(dependencies, ws_workspace_packages)
    )

    names = {dep["name"] for dep in dependencies}
    assert names == {"my_project", "my_project_dep"}
    dep_entry = next(d for d in dependencies if d["name"] == "my_project_dep")
    assert dep_entry["editable"] is True
    assert dep_entry["version_or_source"] == f" @ file://{dep_dir.as_posix()}"


def test_transitive_walk_grows_extras_and_re_enqueues(
    tmp_path: pathlib.Path,
) -> None:
    """A node first reached bare and later with an extra is re-walked with the
    union of its extras.

    ``A`` names ``B`` bare and ``C`` names ``B[x]``; with ``C`` queued before
    ``A``, ``A`` is popped first so ``B`` is created bare, then grown by ``C``.
    The extras' own workspace-editable package must then appear in the result —
    a merge-only walk that never re-enqueues would stop at the first reach and
    miss it.
    """
    a_dir = _make_pkg(tmp_path / "A", "A", 'dependencies = ["B"]\n')
    c_dir = _make_pkg(tmp_path / "C", "C", 'dependencies = ["B[x]"]\n')
    b_x_dep_dir = _make_pkg(tmp_path / "b_x_dep", "b_x_dep", "")
    _make_pkg(
        tmp_path / "B",
        "B",
        '[project.optional-dependencies]\nx = ["b_x_dep"]\n',
    )
    ws_workspace_packages = {
        "A": _pkg(a_dir),
        "C": _pkg(c_dir),
        "B": _pkg(tmp_path / "B"),
        "b_x_dep": _pkg(b_x_dep_dir),
    }

    dependencies = [
        make_dep(
            name="C",
            version_or_source=f" @ file://{c_dir.as_posix()}",
            editable=True,
        ),
        make_dep(
            name="A",
            version_or_source=f" @ file://{a_dir.as_posix()}",
            editable=True,
        ),
    ]

    result = collect_transitive_editable_deps(dependencies, ws_workspace_packages)

    b_entries = [dep for dep in result if dep["name"] == "B"]
    assert len(b_entries) == 1
    assert b_entries[0]["extras"] == ["x"]
    assert {dep["name"] for dep in result} == {"B", "b_x_dep"}


def test_transitive_walk_merges_extras_from_project_and_groups(
    tmp_path: pathlib.Path,
) -> None:
    """One walked pyproject naming ``B`` both bare and with an extra resolves the
    union, not first-wins."""
    root_dir = _make_pkg(
        tmp_path / "root",
        "root",
        'dependencies = ["B"]\n[dependency-groups]\ng = ["B[x]"]\n',
    )
    b_x_dep_dir = _make_pkg(tmp_path / "b_x_dep", "b_x_dep", "")
    _make_pkg(
        tmp_path / "B",
        "B",
        '[project.optional-dependencies]\nx = ["b_x_dep"]\n',
    )
    ws_workspace_packages = {
        "root": _pkg(root_dir),
        "B": _pkg(tmp_path / "B"),
        "b_x_dep": _pkg(b_x_dep_dir),
    }

    dependencies = [
        make_dep(
            name="root",
            version_or_source=f" @ file://{root_dir.as_posix()}",
            editable=True,
        )
    ]

    result = collect_transitive_editable_deps(dependencies, ws_workspace_packages)

    b_entry = next(dep for dep in result if dep["name"] == "B")
    assert b_entry["extras"] == ["x"]


def test_transitive_walk_installs_only_editable_packages_from_extra(
    tmp_path: pathlib.Path,
) -> None:
    """An extra's package is collected only when it is workspace-editable; the
    other names it declares are skipped, matching the plain-dependencies path."""
    root_dir = _make_pkg(
        tmp_path / "root",
        "root",
        '[project.optional-dependencies]\nx = ["present", "absent"]\n',
    )
    present_dir = _make_pkg(tmp_path / "present", "present", "")
    ws_workspace_packages = {"root": _pkg(root_dir), "present": _pkg(present_dir)}

    dependencies = [
        make_dep(
            name="root",
            version_or_source=f" @ file://{root_dir.as_posix()}",
            editable=True,
            extras=["x"],
        )
    ]

    result = collect_transitive_editable_deps(dependencies, ws_workspace_packages)

    assert {dep["name"] for dep in result} == {"present"}


def test_workspace_package_ref_uses_wheel_when_present(
    tmp_path: pathlib.Path,
) -> None:
    """A package with a built wheel is referenced by that wheel, non-editable.

    This is what makes wheel mode install the artifact built from the checkout
    instead of re-running the build backend for every env.
    """
    wheel = tmp_path / "present-1.0.0-py3-none-any.whl"

    version_or_source, editable = workspace_package_ref(
        "present", _pkg(tmp_path, wheel, editable=False)
    )

    assert version_or_source == f" @ file://{wheel.as_posix()}"
    assert editable is False


def test_workspace_package_ref_errors_when_wheel_missing(
    tmp_path: pathlib.Path,
) -> None:
    """A package that must be installed from a wheel but has none is reported,
    never silently reverted to an editable install.

    A silent revert would reintroduce the per-env build cost wheel mode exists
    to remove, and would make the env test different code than the wheelhouse
    recorded (P5/R4).
    """
    with pytest.raises(code_action.ActionFailedException) as exc_info:
        workspace_package_ref("present", _pkg(tmp_path, editable=False))

    assert "present" in str(exc_info.value)
    assert "prepare-envs" in str(exc_info.value)


def test_transitive_walk_reads_source_but_emits_wheel(
    tmp_path: pathlib.Path,
) -> None:
    """The transitive walk reads a package's source pyproject.toml to find its
    workspace dependencies, but emits each dependency's wheel reference.

    Reading the source is required to discover the dependency edges at all;
    emitting the wheel is what the install then uses, so wheel mode still walks
    the full workspace graph rather than stopping at the first wheel.
    """
    root_dir = _make_pkg(tmp_path / "root", "root", 'dependencies = ["present"]\n')
    present_dir = _make_pkg(tmp_path / "present", "present", "")
    root_wheel = tmp_path / "root-1.0.0-py3-none-any.whl"
    present_wheel = tmp_path / "present-1.0.0-py3-none-any.whl"
    ws_workspace_packages = {
        "root": _pkg(root_dir, root_wheel, editable=False),
        "present": _pkg(present_dir, present_wheel, editable=False),
    }
    dependencies = [
        make_dep(
            name="root",
            version_or_source=f" @ file://{root_wheel.as_posix()}",
            editable=False,
        )
    ]

    result = collect_transitive_editable_deps(dependencies, ws_workspace_packages)

    present = next(dep for dep in result if dep["name"] == "present")
    assert present["version_or_source"] == f" @ file://{present_wheel.as_posix()}"
    assert present["editable"] is False
