import typing

import cattrs
from finecode_extension_api.interfaces import iserviceregistry

from finecode_extension_runner._converter import converter as _converter
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.service_config import ServiceConfigResolver

T = typing.TypeVar("T")


class ServiceRegistry(iserviceregistry.IServiceRegistry):
    def __init__(
        self,
        di_registry: Registry,
        config_resolver: ServiceConfigResolver | None = None,
    ) -> None:
        self._di_registry = di_registry
        self._config_resolver = config_resolver
        # Types bound as their own interface. Such a type owns its binding, so it
        # must never be re-pointed at another interface's instance by the
        # concrete-type aliasing below.
        self._self_bound_types: set[type] = set()

    def register_impl(
        self,
        interface: type[T],
        impl: type[T],
        raw_config: dict | None = None,
    ) -> None:
        """Bind ``interface`` to ``impl``."""
        # Resolve config here rather than inside the factory so that every
        # binding picks up declaration config and env overrides regardless of
        # who registered it -- an activator, a `[[tool.finecode.service]]`
        # entry, or the runner's own bootstrap (ADR-0070).
        if self._config_resolver is not None:
            raw_config = self._config_resolver.resolve(interface, raw_config)

        async def factory(registry) -> T:
            from finecode_extension_runner._services.run_action import (
                ActionFailedException,
                _format_validation_error,
                resolve_func_args_with_di,
            )

            def get_service_config(param_type):
                try:
                    return _converter.structure(raw_config or {}, param_type)
                # BaseValidationError, not ClassValidationError: a malformed
                # entry in a list- or dict-typed config field raises
                # IterableValidationError, which is a sibling of
                # ClassValidationError rather than a subclass.
                except cattrs.BaseValidationError as exception:
                    raise ActionFailedException(
                        f"Invalid config for service "
                        f"'{interface.__module__}.{interface.__qualname__}': "
                        f"{_format_validation_error(exception)}"
                    ) from exception

            args = await resolve_func_args_with_di(
                impl.__init__,
                params_to_ignore=["self"],
                registry=registry,
                known_args={"config": get_service_config},
            )
            return impl(**args)

        self._di_registry.register_factory(interface, factory)

        if interface is impl:
            self._self_bound_types.add(impl)
            return

        if impl not in self._self_bound_types:

            async def through_factory(registry) -> T:
                return await registry.get_instance(interface)

            self._di_registry.register_factory(impl, through_factory)
