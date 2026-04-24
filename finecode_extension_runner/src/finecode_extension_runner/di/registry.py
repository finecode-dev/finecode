import inspect
from typing import Any, Callable, Type, TypeVar

T = TypeVar("T")


class Registry:
    def __init__(self) -> None:
        self._container: dict[type, Any] = {}
        self._factories: dict[type, Callable] = {}

    def register_instance(self, type_: type, instance: Any, *, override: bool = False) -> None:
        if type_ in self._container and not override:
            raise ValueError(
                f"Instance for {type_} is already registered. Use override=True to replace it."
            )
        self._container[type_] = instance

    def register_factory(self, type_: type, factory: Callable) -> None:
        self._factories[type_] = factory

    async def get_instance(self, type_: Type[T]) -> T:
        if type_ in self._container:
            return self._container[type_]

        if type_ not in self._factories:
            raise ValueError(f"No implementation found for {type_}")

        factory_result = self._factories[type_](self)

        if inspect.isawaitable(factory_result):
            instance = await factory_result
        else:
            instance = factory_result

        from finecode_extension_api import service
        if isinstance(instance, service.Service):
            await instance.init()

        self._container[type_] = instance
        return instance
