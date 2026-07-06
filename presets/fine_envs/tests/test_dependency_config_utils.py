import pathlib

from fine_envs.dependency_config_utils import (
    collect_transitive_editable_deps,
    resolve_install_project,
)


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
    ws_editable_packages = {"my_project": project_dir, "my_project_dep": dep_dir}

    dependencies = resolve_install_project([], "my_project", project_dir)
    dependencies.extend(
        collect_transitive_editable_deps(dependencies, ws_editable_packages)
    )

    names = {dep["name"] for dep in dependencies}
    assert names == {"my_project", "my_project_dep"}
    dep_entry = next(d for d in dependencies if d["name"] == "my_project_dep")
    assert dep_entry["editable"] is True
    assert dep_entry["version_or_source"] == f" @ file://{dep_dir.as_posix()}"
