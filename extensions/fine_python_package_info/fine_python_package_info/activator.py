from fine_python_package_info import (
    ipypackagelayoutinfoprovider,
    py_package_layout_info_provider,
    py_src_artifact_file_classifier,
)
from finecode_extension_api import extension
from finecode_extension_api.interfaces import iserviceregistry, isrcartifactfileclassifier


class Activator(extension.ExtensionActivator):
    def __init__(self, registry: iserviceregistry.IServiceRegistry) -> None:
        self.registry = registry

    def activate(self) -> None:
        self.registry.register_impl(
            ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider,
            py_package_layout_info_provider.PyPackageLayoutInfoProvider,
        )
        self.registry.register_impl(
            isrcartifactfileclassifier.ISrcArtifactFileClassifier,
            py_src_artifact_file_classifier.PySrcArtifactFileClassifier,
        )
