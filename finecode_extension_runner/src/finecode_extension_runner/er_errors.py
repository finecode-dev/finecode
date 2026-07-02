"""ER-internal exception vocabulary.

These exceptions represent failures at the ER's own layer boundaries. They are
not part of any public interface contract and must be translated to interface-level
exceptions (e.g. ProjectInfoUnavailableError) before crossing into higher layers.
"""


class WmCommunicationError(Exception):
    """Raised when a request to the Workspace Manager fails or times out."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class WmCommunicationCancelled(Exception):
    """Raised when a request to the Workspace Manager was cancelled rather
    than failing — the WM signalled a genuine cancellation, not a
    communication failure."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PackageNotInstalledError(Exception):
    """Raised when a Python module required during ``update_config`` is not installed in the env.
    """

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        super().__init__(f"No module named '{module_name}'")
