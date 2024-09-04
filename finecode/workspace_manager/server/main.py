from modapp import Modapp
from modapp.converters.json import JsonConverter

from modapp.transports.web_socketify import WebSocketifyTransport
from modapp.transports.web_socketify_config import WebSocketifyTransportConfig
from loguru import logger
import sys
logger.remove()
logger.add(sys.stdout, level="TRACE")

from .api_routes import router

def create_manager_app() -> Modapp:
    app = Modapp(
        set(
            [
                WebSocketifyTransport(
                    config=WebSocketifyTransportConfig(port=0),
                    converter=JsonConverter(),
                )
            ],
        ),
    )

    app.include_router(router)
    return app
