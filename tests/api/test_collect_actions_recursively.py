from pathlib import Path
from finecode.api import collect_actions_recursively


def test__collection_actions_recursively__builds_deep_tree_correctly():
    nested_package_path = Path(__file__).parent.parent / "nested_package"

    nested_package = collect_actions_recursively(root_dir=nested_package_path)

    assert nested_package.name == "nested_package"
    assert nested_package.path == nested_package_path
    assert nested_package.actions == []
    pyback_package = next(
        package for package in nested_package.subpackages if package.name == "pyback"
    )
    assert pyback_package.name == "pyback"
    assert pyback_package.path == nested_package.path / "pyback"
    assert pyback_package.actions == []
    jsfront_package = next(
        package for package in pyback_package.subpackages if package.name == "jsfront"
    )
    assert jsfront_package.name == "jsfront"
    assert jsfront_package.path == pyback_package.path / "jsfront"
    # TODO: assert actions
    anotherlevel_package = next(
        package
        for package in jsfront_package.subpackages
        if package.name == "anotherlevel"
    )
    assert anotherlevel_package.name == "anotherlevel"
    assert anotherlevel_package.path == jsfront_package.path / "anotherlevel"
    assert anotherlevel_package.actions == []
    generalpackage_package = next(
        package
        for package in anotherlevel_package.subpackages
        if package.name == "generalpackage"
    )
    assert generalpackage_package.name == "generalpackage"
    assert generalpackage_package.path == anotherlevel_package.path / "generalpackage"
    # TODO: assert actions
