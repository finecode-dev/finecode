from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry

from fine_python_ruff.ruff_lsp_service import RuffLspService


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(
            RuffLspService,
            RuffLspService,
        )
