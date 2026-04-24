import typing

from finecode_extension_api.interfaces import iserviceregistry
from finecode_extension_runner.di.registry import Registry

T = typing.TypeVar("T")


class ServiceRegistry(iserviceregistry.IServiceRegistry):
    def __init__(self, di_registry: Registry) -> None:
        self._di_registry = di_registry

    def register_impl(
        self, interface: type[T], impl: type[T], singleton: bool = False
    ) -> None:
        async def factory(registry) -> T:
            from finecode_extension_runner._services.run_action import (
                resolve_func_args_with_di,
            )

            args = await resolve_func_args_with_di(
                impl.__init__, params_to_ignore=["self"], registry=registry
            )
            return impl(**args)

        self._di_registry.register_factory(interface, factory)
