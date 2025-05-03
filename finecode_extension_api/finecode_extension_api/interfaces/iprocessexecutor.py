import typing
from asyncio import BaseProtocol

T = typing.TypeVar("T")
P = typing.ParamSpec("P")


class IProcessExecutor(BaseProtocol):
    async def submit(
        self, func: typing.Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ): ...
