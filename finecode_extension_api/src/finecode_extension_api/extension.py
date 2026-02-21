import typing


class ExtensionActivator(typing.Protocol):
    """Protocol for extension activation."""

    def activate(self) -> None:
        """Called when extension is loaded."""
        ...
