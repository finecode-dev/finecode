import pytest

from .extension_runner.fixtures import runner_client_channel
from .workspace_manager.server.fixtures import client_channel


@pytest.fixture
def anyio_backend():
    return "asyncio"
