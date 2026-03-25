# docs: docs/concepts.md, docs/guides/creating-extension.md
import sys
import typing

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


@typing.runtime_checkable
class Service(typing.Protocol):
    async def init(self) -> None: ...


@typing.runtime_checkable
class DisposableService(Service, typing.Protocol):
    @override
    async def init(self) -> None: ...

    def dispose(self) -> None: ...
