# import sys

# from loguru import logger
from modapp import Modapp
from modapp.converters.json import JsonConverter
from modapp.transports.web_aiohttp import WebAiohttpTransport
from modapp.transports.web_aiohttp_config import WebAiohttpTransportConfig

# logger.remove()
# logger.add(sys.stdout, level="TRACE")

from .api_routes import router


def create_manager_app() -> Modapp:
    app = Modapp(
            [
                WebAiohttpTransport(
                    config=WebAiohttpTransportConfig(port=0),
                    converter=JsonConverter(),
                )
            ],
        keep_running_endpoint=True
    )

    app.include_router(router)
    return app
