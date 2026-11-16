from __future__ import annotations

import asyncio

from finecode.wm_server.services.run_service import proxy_utils


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _collect(async_list: proxy_utils.AsyncList[str]) -> list[str]:
    return [item async for item in async_list]


async def test_item_read_without_waiting_does_not_end_open_stream() -> None:
    """Reading an already-appended item leaves its change signal set; the next
    empty read must keep waiting instead of treating that stale signal as the
    end of the stream (#915).
    """
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    async_list.append("first")
    reader = asyncio.create_task(_collect(async_list))
    await _settle()

    assert not reader.done()

    async_list.append("second")
    async_list.end()

    assert await reader == ["first", "second"]


async def test_empty_wake_does_not_end_open_stream() -> None:
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    reader = asyncio.create_task(_collect(async_list))
    await _settle()

    async_list.change_event.set()
    await _settle()

    assert not reader.done()

    async_list.append("item")
    async_list.end()

    assert await reader == ["item"]


async def test_end_drains_unread_items_then_stops() -> None:
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    reader = asyncio.create_task(_collect(async_list))
    await _settle()

    async_list.append("a")
    async_list.append("b")
    async_list.end()

    assert await reader == ["a", "b"]


async def test_end_of_empty_stream_stops_pending_reader() -> None:
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    reader = asyncio.create_task(_collect(async_list))
    await _settle()

    async_list.end()

    assert await reader == []


async def test_readers_keep_independent_positions() -> None:
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    async_list.append("a")
    first = asyncio.create_task(_collect(async_list))
    await _settle()
    second = asyncio.create_task(_collect(async_list))
    await _settle()

    async_list.append("b")
    async_list.end()

    assert await first == ["a", "b"]
    assert await second == ["a", "b"]


async def test_cancelling_pending_reader_releases_it() -> None:
    async_list: proxy_utils.AsyncList[str] = proxy_utils.AsyncList()
    reader = asyncio.create_task(_collect(async_list))
    await _settle()

    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)

    assert reader.cancelled()
    assert not async_list.change_event._waiters
