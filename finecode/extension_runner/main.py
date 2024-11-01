import socket
from contextlib import closing

from loguru import logger
from modapp import Modapp
from modapp.converters.json import JsonConverter

from modapp.transports.web_socketify import WebSocketifyTransport
from modapp.transports.web_socketify_config import WebSocketifyTransportConfig

import finecode.extension_runner.run_utils as run_utils
from .api_routes import router


def create_extension_app() -> Modapp:
    # find free port
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("localhost", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        free_port = s.getsockname()[1]
    
    # create modapp app
    app = Modapp(
        set(
            [
                WebSocketifyTransport(
                    config=WebSocketifyTransportConfig(port=free_port),
                    converter=JsonConverter(),
                )
            ],
        ),
    )

    app.include_router(router)
    logger.trace(f'Start extension runner in venv {run_utils.get_current_venv_path()}')
    return app
