from pathlib import Path

import pytest

from finecode import workspace_context
from finecode.api._read_configs import read_configs


@pytest.fixture
def nested_package_ws_context():
    ws_context = workspace_context.WorkspaceContext(
        ws_dirs_pathes=[Path(__file__).parent.parent / "nested_package"]
    )
    return ws_context


def test__read_configs__reads_py_packages_with_finecode(
    nested_package_ws_context: workspace_context.WorkspaceContext,
):
    read_configs(ws_context=nested_package_ws_context)

    ...


def test__read_configs__reads_py_packages_without_finecode():
    ...


def test__read_configs__saves_raw_configs():
    ...
