from pathlib import Path

import pytest

from finecode.workspace_manager import context
from finecode.workspace_manager.read_configs import read_configs


@pytest.fixture
def nested_project_ws_context():
    ws_context = context.WorkspaceContext(
        ws_dirs_pathes=[Path(__file__).parent.parent / "nested_package"]
    )
    return ws_context


def test__read_configs__reads_py_packages_with_finecode(
    nested_project_ws_context: context.WorkspaceContext,
):
    read_configs(ws_context=nested_project_ws_context)

    ...


def test__read_configs__reads_py_packages_without_finecode(): ...


def test__read_configs__saves_raw_configs(): ...
