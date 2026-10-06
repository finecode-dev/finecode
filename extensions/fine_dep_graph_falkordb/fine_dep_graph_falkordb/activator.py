from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry

from fine_dep_graph_falkordb.falkordb_client_provider import FalkorDBClientProvider
from fine_dep_graph_falkordb.ifalkordb_client_provider import IFalkorDBClientProvider


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(IFalkorDBClientProvider, FalkorDBClientProvider)
