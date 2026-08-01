from __future__ import annotations

from finecode_extension_runner.di.registry import Registry


class IThing:
    pass


class DisposableThing(IThing):
    def __init__(self) -> None:
        self.disposed = False

    async def init(self) -> None:
        pass

    def dispose(self) -> None:
        self.disposed = True


async def test_evicted_instance_is_rebuilt_on_next_request() -> None:
    # A disposed service must not stay cached: the next handler asking for the
    # interface would be handed an object whose dispose() has already run.
    registry = Registry()
    registry.register_factory(IThing, lambda _registry: DisposableThing())

    first = await registry.get_instance(IThing)
    assert await registry.get_instance(IThing) is first

    first.dispose()
    registry.evict_instance(first)

    second = await registry.get_instance(IThing)
    assert second is not first
    assert not second.disposed


async def test_evict_drops_every_type_the_instance_was_cached_under() -> None:
    # An instance is cached under both the interface and its alias-bound
    # concrete type; evicting only one key would still serve the disposed object.
    registry = Registry()
    registry.register_factory(IThing, lambda _registry: DisposableThing())

    async def through_factory(reg):
        return await reg.get_instance(IThing)

    registry.register_factory(DisposableThing, through_factory)

    via_interface = await registry.get_instance(IThing)
    via_concrete = await registry.get_instance(DisposableThing)
    assert via_interface is via_concrete

    registry.evict_instance(via_interface)

    assert await registry.get_instance(DisposableThing) is not via_interface


async def test_dispose_all_disposes_and_clears_resolved_services() -> None:
    # An on-the-fly config update retires a whole registry; nothing it resolved
    # should survive it.
    registry = Registry()
    registry.register_factory(IThing, lambda _registry: DisposableThing())
    instance = await registry.get_instance(IThing)

    registry.dispose_all()

    assert instance.disposed
    assert await registry.get_instance(IThing) is not instance


async def test_dispose_all_continues_after_one_service_raises() -> None:
    class Exploding(DisposableThing):
        def dispose(self) -> None:
            raise RuntimeError("boom")

    class OtherThing(IThing):
        pass

    registry = Registry()
    registry.register_factory(Exploding, lambda _registry: Exploding())
    registry.register_factory(IThing, lambda _registry: DisposableThing())
    await registry.get_instance(Exploding)
    survivor = await registry.get_instance(IThing)

    registry.dispose_all()

    assert survivor.disposed
