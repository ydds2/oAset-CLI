"""Minimal experiment: create_task + queue + async-generator under pytest-asyncio."""
from __future__ import annotations

import asyncio


async def gen():
    queue: asyncio.Queue = asyncio.Queue()

    async def producer():
        for i in range(3):
            await asyncio.sleep(0)
            await queue.put({"i": i})
        await queue.put(None)

    task = asyncio.create_task(producer())
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


async def test_micro_gen_works():
    seen = []
    async for item in gen():
        seen.append(item["i"])
    assert seen == [0, 1, 2]
