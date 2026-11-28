from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry

from fine_inspect_code.ratchet import IDiagnosticsRatchet
from fine_inspect_code.ratchet_impl import DiagnosticsRatchet


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(
            IDiagnosticsRatchet,
            DiagnosticsRatchet,
        )
