"""Per-client sliding-window rate limiting for the search endpoints.

Protects DelayBahn itself from a single client hammering /api/journeys; the
upstream protections in bahn_api (cache, coalescing, circuit breaker) protect
bahn.de. A client over its budget gets a 429 from *us*, which is distinct from
bahn.de throttling us (surfaced as 503 after the stale fallback).

State is in-memory and per-process: the app runs as a single uvicorn worker,
so the budgets are effectively global. With multiple workers each process
would enforce its own, proportionally looser, limit.
"""

import time
from collections import OrderedDict, deque
from math import ceil
from typing import Callable

# ceiling on tracked clients so a spray of unique addresses stays bounded,
# as in feedback._hits
MAX_CLIENTS = 4096


class SlidingWindowLimiter:
    """Two sliding windows per client: a short burst budget and a longer
    sustained budget. Rejected attempts are not recorded, so a client recovers
    as soon as its window drains instead of being punished into the future."""

    def __init__(self, burst_limit: int, burst_window: float,
                 sustained_limit: int, sustained_window: float,
                 max_clients: int = MAX_CLIENTS,
                 clock: Callable[[], float] = time.monotonic):
        self._burst_limit = burst_limit
        self._burst_window = burst_window
        self._sustained_limit = sustained_limit
        self._sustained_window = sustained_window
        self._max_clients = max_clients
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def retry_after(self, key: str) -> int | None:
        """None when the request is allowed (and recorded); otherwise whole
        seconds until the client's earliest budget frees up."""
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        self._hits.move_to_end(key)
        while hits and now - hits[0] > self._sustained_window:
            hits.popleft()
        if len(hits) >= self._sustained_limit:
            return max(1, ceil(hits[0] + self._sustained_window - now))
        burst_start = now - self._burst_window
        burst = [t for t in hits if t > burst_start]
        if len(burst) >= self._burst_limit:
            return max(1, ceil(burst[0] + self._burst_window - now))
        hits.append(now)
        while len(self._hits) > self._max_clients:
            self._hits.popitem(last=False)
        return None
