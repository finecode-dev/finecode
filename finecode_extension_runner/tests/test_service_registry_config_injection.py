"""Test config injection in services.
"""
from __future__ import annotations

import dataclasses

from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.impls.service_registry import ServiceRegistry


@dataclasses.dataclass
class _FakeServiceConfig:
    max_items: int = 1


class _FakeServiceInterface:
    """Stand-in for a DI interface (structural typing — no ABC needed)."""


class _FakeService(_FakeServiceInterface):
    def __init__(self, config: _FakeServiceConfig) -> None:
        self.config = config


async def test_register_impl_structures_raw_config_into_constructor_config() -> None:
    registry = Registry()
    svc_registry = ServiceRegistry(di_registry=registry)

    svc_registry.register_impl(
        _FakeServiceInterface, _FakeService, raw_config={"max_items": 5}
    )

    instance = await registry.get_instance(_FakeServiceInterface)

    assert isinstance(instance, _FakeService)
    assert instance.config == _FakeServiceConfig(max_items=5)


async def test_register_impl_uses_empty_config_when_raw_config_is_none() -> None:
    registry = Registry()
    svc_registry = ServiceRegistry(di_registry=registry)

    svc_registry.register_impl(_FakeServiceInterface, _FakeService)

    instance = await registry.get_instance(_FakeServiceInterface)

    assert instance.config == _FakeServiceConfig()
