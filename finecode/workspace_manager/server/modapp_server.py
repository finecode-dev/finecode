from modapp import Modapp
from modapp.converters.json import JsonConverter
from modapp.transports.web_aiohttp import WebAiohttpTransport
from modapp.transports.web_aiohttp_config import WebAiohttpTransportConfig


def create_manager_app() -> Modapp:
    from .api_routes import router

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


__all__ = ['create_manager_app']