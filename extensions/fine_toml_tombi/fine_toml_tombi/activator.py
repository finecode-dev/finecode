from fine_toml_tombi.tombi_lsp_service import TombiLspService
from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(
            TombiLspService,
            TombiLspService,
        )
