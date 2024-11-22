from loguru import logger
from modapp import Modapp
from modapp.converters.json import JsonConverter
from modapp.transports.web_aiohttp import WebAiohttpTransport
from modapp.transports.web_aiohttp_config import WebAiohttpTransportConfig

import finecode.extension_runner.run_utils as run_utils

from .api_routes import router


def create_extension_app() -> Modapp:
    # create modapp app
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
    logger.trace(f"Start extension runner in venv {run_utils.get_current_venv_path()}")
    return app
