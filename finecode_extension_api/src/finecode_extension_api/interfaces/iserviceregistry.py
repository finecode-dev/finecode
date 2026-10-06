import typing

T = typing.TypeVar("T")


class IServiceRegistry(typing.Protocol):
    def register_impl(
        self,
        interface: type[T],
        impl: type[T],
        raw_config: dict | None = None,
    ) -> None:
        """Bind ``interface`` to ``impl``.

        ``impl`` is constructed lazily on first injection and cached for the
        Extension Runner's lifetime, so every binding is a singleton.

        ``raw_config`` seeds the implementation's ``config`` constructor
        parameter. Config declared in ``[[tool.finecode.service]]`` and any
        environment-variable overrides are merged into it by the runner, so an
        activator normally passes nothing here and still gets a configurable
        service.
        """
        ...
