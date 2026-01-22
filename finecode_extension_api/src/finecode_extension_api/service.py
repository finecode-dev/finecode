import typing


@typing.runtime_checkable
class Service(typing.Protocol):
    async def init(self) -> None: ...


@typing.runtime_checkable
class DisposableService(Service, typing.Protocol):
    @typing.override
    async def init(self) -> None: ...

    async def dispose(self) -> None: ...
