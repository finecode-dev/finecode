import typing

T = typing.TypeVar("T")
P = typing.ParamSpec("P")


class IProcessExecutor(typing.Protocol):
    async def submit(
        self, func: typing.Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ): ...
