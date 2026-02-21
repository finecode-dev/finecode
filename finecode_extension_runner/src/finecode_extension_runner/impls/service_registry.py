import typing

from finecode_extension_api.interfaces import iserviceregistry
from finecode_extension_runner.di import _state

T = typing.TypeVar("T")


class ServiceRegistry(iserviceregistry.IServiceRegistry):
    def register_impl(
        self, interface: type[T], impl: type[T], singleton: bool = False
    ) -> None:
        async def factory(container: dict) -> T:
            from finecode_extension_runner._services.run_action import (
                resolve_func_args_with_di,
            )

            args = await resolve_func_args_with_di(
                impl.__init__, params_to_ignore=["self"]
            )
            return impl(**args)

        _state.factories[interface] = factory
