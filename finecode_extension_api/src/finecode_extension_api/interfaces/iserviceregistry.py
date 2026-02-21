import typing

T = typing.TypeVar("T")


class IServiceRegistry(typing.Protocol):
    def register_impl(
        self, interface: type[T], impl: type[T], singleton: bool = False
    ) -> None: ...
