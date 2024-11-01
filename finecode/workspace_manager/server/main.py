import sys

from loguru import logger
from modapp import Modapp
from modapp.converters.json import JsonConverter
# from modapp.transports.web_socketify import WebSocketifyTransport
# from modapp.transports.web_socketify_config import WebSocketifyTransportConfig
from modapp.transports.web_aiohttp import WebAiohttpTransport
from modapp.transports.web_aiohttp_config import WebAiohttpTransportConfig

logger.remove()
logger.add(sys.stdout, level="TRACE")

from .api_routes import router


def create_manager_app() -> Modapp:
    app = Modapp(
        set(
            [
                # WebSocketifyTransport(
                #     config=WebSocketifyTransportConfig(port=0),
                #     converter=JsonConverter(),
                # )
                WebAiohttpTransport(
                    config=WebAiohttpTransportConfig(port=0),
                    converter=JsonConverter(),
                )
            ],
        ),
    )

    app.include_router(router)
    return app
