import asyncio
from modapp import Modapp
from modapp.client import Client
from modapp.transports.inmemory import InMemoryTransport
from modapp.transports.inmemory_config import InMemoryTransportConfig
from modapp.channels.inmemory import InMemoryChannel
from modapp.converters.json import JsonConverter
import pytest

from finecode.workspace_manager.server.api_routes import router


def _create_manager_app() -> Modapp:
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
def client_channel():
    app = _create_manager_app()
    json_converter = JsonConverter()
    try:
        inmemory_transport = next(
            transport
            for transport in app.transports
            if isinstance(transport, InMemoryTransport)
        )
    except StopIteration:
        raise Exception("App configuration error. InMemory transport not found")
    channel = InMemoryChannel(
        transport=inmemory_transport, converter=json_converter
    )
    client = Client(channel=channel)
    
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(app.run_async())
    
    try:
        yield client.channel
    finally:
        app.stop()
