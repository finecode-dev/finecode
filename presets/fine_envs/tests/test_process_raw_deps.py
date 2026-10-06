import pathlib

from fine_envs.dependency_config_utils import process_raw_deps


def _run(raw_deps: list, deps_groups: dict | None = None) -> list[dict]:
    dependencies: list[dict] = []
    process_raw_deps(
        raw_deps,
        dependencies,
        deps_groups or {},
        pathlib.Path("/unused/pyproject.toml"),
    )
    return dependencies


def test_process_raw_deps_splits_extras_out_of_version() -> None:
    """A bracketed spec separates extras from the version specifier.

    Without this split the bracket group would ride inside
    ``version_or_source``, where the editable rewrite later overwrites it and
    the backend would never see it.
    """
    deps = _run(["pkg[a,b]~=1.0"])

    assert len(deps) == 1
    dep = deps[0]
    assert dep["name"] == "pkg"
    assert dep["extras"] == ["a", "b"]
    assert dep["version_or_source"] == "~=1.0"


def test_process_raw_deps_without_extras_has_empty_extras() -> None:
    deps = _run(["pkg~=1.0"])

    assert deps[0]["extras"] == []
    assert deps[0]["version_or_source"] == "~=1.0"


def test_process_raw_deps_merges_extras_across_included_groups() -> None:
    """Two references to the same distribution keep the union of their extras.

    The first-wins dedup must not drop a later, extras-bearing reference —
    otherwise a bare ``B~=1.0`` in one group would silently discard the
    ``[x]`` selected by another group.
    """
    deps_groups = {
        "base": ["B~=1.0"],
        "extra": ["B[x]~=1.0"],
    }

    deps = _run(
        [{"include-group": "base"}, {"include-group": "extra"}],
        deps_groups,
    )

    assert len(deps) == 1
    assert deps[0]["name"] == "B"
    assert deps[0]["extras"] == ["x"]
