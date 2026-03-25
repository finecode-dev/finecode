from fine_python_mypy import ast_provider, iast_provider
from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(
            iast_provider.IMypySingleAstProvider, ast_provider.MypySingleAstProvider
        )
