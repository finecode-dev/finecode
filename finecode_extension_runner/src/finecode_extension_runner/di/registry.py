import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class ServiceNotFoundError(ValueError):
    """Raised when no implementation is registered for a requested type."""


class Registry:
    def __init__(self) -> None:
        self._container: dict[type, Any] = {}
        self._factories: dict[type, Callable] = {}
        self._deferred_activators: list[Callable[[], None]] = []
        self._next_deferred: int = 0

    def set_deferred_activators(self, activators: list[Callable[[], None]]) -> None:
        self._deferred_activators = activators
        self._next_deferred = 0

    def register_instance(
        self, type_: type, instance: Any, *, override: bool = False
    ) -> None:
        if type_ in self._container and not override:
            raise ValueError(
                f"Instance for {type_} is already registered. Use override=True to replace it."
            )
        self._container[type_] = instance

    def register_factory(self, type_: type, factory: Callable) -> None:
        self._factories[type_] = factory

    def evict_instance(self, instance: Any) -> None:
        """Drop every cached binding pointing at ``instance``.

        Called when a service is disposed: the factory stays registered, so the
        next request rebuilds it. Without this the registry keeps handing out an
        object whose ``dispose()`` has already run. One instance can be cached
        under several types (an interface and its alias-bound concrete class), so
        every key is checked rather than just the one the caller knows about.
        """
        for type_ in [t for t, cached in self._container.items() if cached is instance]:
            del self._container[type_]

    def dispose_all(self) -> None:
        """Dispose and drop every resolved service.

        Used when a whole registry is being retired -- an on-the-fly config
        update builds a replacement, and without this the outgoing registry's
        services (LSP server subprocesses among them) stay alive with nothing
        referencing them.
        """
        from finecode_extension_api import service

        for instance in list(self._container.values()):
            if isinstance(instance, service.DisposableService):
                try:
                    instance.dispose()
                except Exception:
                    # Best-effort: one service failing to dispose must not strand
                    # the rest, and the registry is being discarded regardless.
                    logger.exception(f"Failed to dispose service: {instance}")
        self._container.clear()

    async def get_instance(self, type_: type[T]) -> T:
        if type_ in self._container:
            return self._container[type_]

        if type_ not in self._factories:
            while self._next_deferred < len(self._deferred_activators):
                activate = self._deferred_activators[self._next_deferred]
                self._next_deferred += 1
                activate()
                if type_ in self._factories:
                    break
        if type_ not in self._factories:
            raise ServiceNotFoundError(f"No implementation found for {type_}")

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
