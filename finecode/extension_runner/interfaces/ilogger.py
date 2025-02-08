from typing import Protocol


class ILogger(Protocol):
    def info(self, message: str) -> None:
        ...
    
    def debug(self, message: str) -> None:
        ...

    def disable(self, package: str) -> None:
        ...
    
    def enable(self, package: str) -> None:
        ...
