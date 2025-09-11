from typing import Type, TypeVar
import inspect

from finecode_extension_api import code_action, service

from ._state import container, factories

T = TypeVar("T")


async def get_service_instance(service_type: Type[T]) -> T:
    if service_type == code_action.ActionHandlerLifecycle:
        return code_action.ActionHandlerLifecycle()

    # singletons
    if service_type in container:
        return container[service_type]
    else:
        if service_type in factories:
            factory_result = factories[service_type](container)
        else:
            raise ValueError(f"No implementation found for {service_type}")

        if inspect.isawaitable(factory_result):
            service_instance = await factory_result
        else:
            service_instance = factory_result

        if isinstance(service_instance, service.Service):
            await service_instance.init()

        container[service_type] = service_instance
        return service_instance
