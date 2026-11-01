from __future__ import annotations

import asyncio
import logging
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


async def start_with_retry(factory: Callable[[], T], attempts: int = 60, delay: float = 1.0) -> T:
    """Build and start a Kafka client, retrying while the broker comes up.

    A client that failed to start is not reusable, so each attempt builds a fresh one.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = factory()
        try:
            await client.start()  # type: ignore[attr-defined]
            return client
        except Exception as exc:  # broker not ready yet
            last = exc
            log.warning("kafka not ready (%s), attempt %d/%d", exc.__class__.__name__, attempt, attempts)
            try:
                await client.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
            await asyncio.sleep(delay)
    raise RuntimeError(f"could not reach kafka after {attempts} attempts") from last
