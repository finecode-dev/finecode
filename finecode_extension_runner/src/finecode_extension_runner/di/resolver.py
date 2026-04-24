from typing import Any, Type, TypeVar

from finecode_extension_api import code_action

T = TypeVar("T")


async def get_service_instance(service_type: Type[T], registry: Any) -> T:
    if service_type == code_action.ActionHandlerLifecycle:
        return code_action.ActionHandlerLifecycle()
    return await registry.get_instance(service_type)
