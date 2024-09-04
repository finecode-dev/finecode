from modapp.channels.httpx import HttpxChannel
from modapp.client import Client
from modapp.converters.json import JsonConverter


def create_client(server_address: str):
    json_converter = JsonConverter()
    channel = HttpxChannel(converter=json_converter, server_address=server_address)
    client = Client(channel=channel)
    return client
