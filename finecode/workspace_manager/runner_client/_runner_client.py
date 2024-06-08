from modapp.channels.aiohttp import AioHttpChannel
from modapp.client import Client
from modapp.converters.json import JsonConverter
from modapp.validators.dataclass import DataclassValidator


def create_client(server_address: str):
    json_converter = JsonConverter(validator=DataclassValidator())
    channel = AioHttpChannel(converter=json_converter, server_address=server_address)
    client = Client(channel=channel)
    return client
