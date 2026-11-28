"""ER-internal exception vocabulary.

These exceptions represent failures at the ER's own layer boundaries. They are
not part of any public interface contract and must be translated to interface-level
exceptions (e.g. ProjectInfoUnavailableError) before crossing into higher layers.
"""

import asyncio


class WmCommunicationError(Exception):
    """Raised when a request to the Workspace Manager fails or times out.

    ``subtree_stopped`` carries the delegated child's stop verdict from the
    reply (``False`` is the safe reading when the reply carried none).
    """

    def __init__(self, message: str, *, subtree_stopped: bool = False) -> None:
        self.message = message
        self.subtree_stopped = subtree_stopped
        super().__init__(message)


class WmCommunicationCancelled(Exception):
    """Raised when a request to the Workspace Manager was cancelled rather
    than failing — the WM signalled a genuine cancellation, not a
    communication failure.

    ``subtree_stopped`` carries the delegated child's stop verdict from the
    reply (``False`` is the safe reading when the reply carried none).
    """

    def __init__(self, message: str, *, subtree_stopped: bool = False) -> None:
        self.message = message
        self.subtree_stopped = subtree_stopped
        super().__init__(message)


class WmCallAbandoned(asyncio.CancelledError):
    """Raised when the ER stopped waiting for a WM request it cancelled itself.

    Unlike ``WmCommunicationCancelled`` (the WM cancelled), the ER's own
    handler stopped waiting — its await was cancelled or its iterator was
    closed — and the post-cancel wait for the child's reply has ended.
    ``subtree_stopped`` carries the delegated child's stop verdict from the
    reply (``False`` is the safe reading when the reply carried none).
    """

    def __init__(self, message: str, *, subtree_stopped: bool = False) -> None:
        self.message = message
        self.subtree_stopped = subtree_stopped
        super().__init__(message)


class PackageNotInstalledError(Exception):
    """Raised when a Python module required during ``update_config`` is not installed in the env."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        super().__init__(f"No module named '{module_name}'")
