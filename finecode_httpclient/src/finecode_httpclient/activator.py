from finecode_extension_api import extension
from finecode_extension_api.interfaces import ihttpclient, iserviceregistry

from finecode_httpclient.client import HttpClient


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(ihttpclient.IHttpClient, HttpClient)
