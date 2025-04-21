import typing
from asyncio import BaseProtocol


class IProcessExecutor(BaseProtocol):
    async def submit[T, **P](
        self, func: typing.Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ): ...
