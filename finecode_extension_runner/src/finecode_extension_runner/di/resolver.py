from typing import Any, Callable, Type, TypeVar

from finecode_extension_api import code_action

from ._state import container, factories

T = TypeVar("T")


def get_service_instance(service_type: Type[T]) -> T:
    if service_type == code_action.ActionHandlerLifecycle:
        return code_action.ActionHandlerLifecycle()

    # singletons
    if service_type in container:
        return container[service_type]
    else:
        if service_type in factories:
            service_instance = factories[service_type](container)
        else:
            raise ValueError(f"No implementation found for {service_type}")

        container[service_type] = service_instance
        return service_instance
