"""LRU bytes cache for full DICOM instances + in-flight Future deduplication.

OHIF prefetches aggressively (multiple concurrent requests for adjacent slices),
so two requests for the same instance arriving at once should share a single
fetch rather than racing to S3.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_BUDGET_BYTES = 1024 * 1024 * 1024  # 1 GB


class InstanceCache:
    """LRU cache of full instance bytes, keyed by entity_id."""

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES):
        self._budget = budget_bytes
        self._size = 0
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        self._lock = asyncio.Lock()

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._cache),
            "bytes": self._size,
            "budget": self._budget,
            "inflight": len(self._inflight),
        }

    def _put(self, entity_id: str, data: bytes) -> None:
        if entity_id in self._cache:
            self._size -= len(self._cache[entity_id])
            del self._cache[entity_id]
        self._cache[entity_id] = data
        self._size += len(data)
        while self._size > self._budget and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._size -= len(evicted)

    def _get(self, entity_id: str) -> bytes | None:
        data = self._cache.get(entity_id)
        if data is not None:
            self._cache.move_to_end(entity_id)
        return data

    async def get_or_fetch(
        self,
        entity_id: str,
        fetch: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        """Return cached bytes or fetch them; concurrent callers share a single fetch."""
        async with self._lock:
            cached = self._get(entity_id)
            if cached is not None:
                return cached
            fut = self._inflight.get(entity_id)
            if fut is None:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[entity_id] = fut
                owner = True
            else:
                owner = False

        if not owner:
            return await fut

        try:
            data = await fetch()
        except BaseException as exc:
            async with self._lock:
                self._inflight.pop(entity_id, None)
            if not fut.done():
                fut.set_exception(exc)
            raise

        async with self._lock:
            self._put(entity_id, data)
            self._inflight.pop(entity_id, None)
        if not fut.done():
            fut.set_result(data)
        return data
