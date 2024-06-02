from modapp import Modapp
from modapp.converters.json import JsonConverter

from modapp.validators.dataclass import DataclassValidator
from modapp.transports.web_socketify import WebSocketifyTransport
from modapp.transports.web_socketify_config import WebSocketifyTransportConfig

from .api_routes import router


def create_extension_app() -> Modapp:
    validator = DataclassValidator()
    app = Modapp(
        set(
            [
                WebSocketifyTransport(
                    config=WebSocketifyTransportConfig(),
                    validator=validator,
                    converter=JsonConverter(validator=validator),
                )
            ],
        ),
    )

    app.include_router(router)
    return app
