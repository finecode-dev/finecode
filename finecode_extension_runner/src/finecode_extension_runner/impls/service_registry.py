import typing

import cattrs

from finecode_extension_api.interfaces import iserviceregistry
from finecode_extension_runner._converter import converter as _converter
from finecode_extension_runner.di.registry import Registry

T = typing.TypeVar("T")


class ServiceRegistry(iserviceregistry.IServiceRegistry):
    def __init__(self, di_registry: Registry) -> None:
        self._di_registry = di_registry

    def register_impl(
        self,
        interface: type[T],
        impl: type[T],
        singleton: bool = False,
        raw_config: dict | None = None,
    ) -> None:
        async def factory(registry) -> T:
            from finecode_extension_runner._services.run_action import (
                resolve_func_args_with_di,
            )

            def get_service_config(param_type):
                try:
                    return _converter.structure(raw_config or {}, param_type)
                except cattrs.ClassValidationError as exception:
                    raise ValueError(str(exception)) from exception

            args = await resolve_func_args_with_di(
                impl.__init__,
                params_to_ignore=["self"],
                registry=registry,
                known_args={"config": get_service_config},
            )
            return impl(**args)

        self._di_registry.register_factory(interface, factory)

        if singleton and interface is not impl:
            async def through_factory(registry) -> T:
                return await registry.get_instance(interface)

            self._di_registry.register_factory(impl, through_factory)
