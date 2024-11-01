import pytest
from modapp import Modapp
from modapp.channels.inmemory import InMemoryChannel
from modapp.client import Client
from modapp.converters.json import JsonConverter
from modapp.transports.inmemory import InMemoryTransport
from modapp.transports.inmemory_config import InMemoryTransportConfig

import finecode.workspace_manager.main as workspace_manager_main
from finecode.extension_runner.api_routes import router
from finecode.extension_runner.api_routes import \
    ws_context as global_ws_context

pytestmark = pytest.mark.anyio


def _create_runner_app() -> Modapp:
    app = Modapp(
        set(
            [
                InMemoryTransport(
                    config=InMemoryTransportConfig(),
                    converter=JsonConverter(),
                )
            ],
        ),
    )

    app.include_router(router)
    return app


@pytest.fixture
async def runner_client_channel():
    app = _create_runner_app()
    json_converter = JsonConverter()
    try:
        inmemory_transport = next(
            transport for transport in app.transports if isinstance(transport, InMemoryTransport)
        )
    except StopIteration:
        raise Exception("App configuration error. InMemory transport not found")
    channel = InMemoryChannel(transport=inmemory_transport, converter=json_converter)
    client = Client(channel=channel)

    await workspace_manager_main.start_in_ws_context(global_ws_context)
    await app.run_async()

    try:
        yield client.channel
    finally:
        app.stop()
