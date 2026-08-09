import asyncio
import logging
import os
import threading
from time import monotonic, sleep
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("backend_v2.data.client")

DEFAULT_BASE_URL = "https://api.massive.com/"

# massive.com throttles paged endpoints (e.g. /v3/reference/tickers) hard enough that a
# job paging through thousands of results routinely trips it - retry 429s with backoff
# instead of letting raise_for_status() kill the whole job on the first one.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

# Paged jobs (sync_tickers, sync_bars) fire the next page the instant the previous one
# resolves, with no gap - that alone is enough to trip massive.com's limiter well before
# any single request looks abusive. Spacing requests out here, once, fixes it for every
# caller instead of the retry/backoff above having to paper over it after the fact on
# every page. No published quota to tune this against and massive.com's 429s carry no
# Retry-After/X-RateLimit-* headers to read one off of either - 100ms (10 req/s) is a
# conservative starting point; overridable via env since the right value is a guess
# until massive.com's support responds with their actual limit.
MIN_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MASSIVE_MIN_REQUEST_INTERVAL_SECONDS", "0.1"))

# Global batch throttle on top of the per-instance pacing above: MIN_REQUEST_INTERVAL_SECONDS
# only paces requests within a single DataClient, but sync jobs (sync_bars.py etc.) fan
# out one DataClient per ThreadPoolExecutor worker, each in its own thread with its own
# event loop - so N workers can still fire ~N near-simultaneous requests regardless of
# per-client pacing. This caps total throughput across every thread in the process:
# RATE_LIMIT_BATCH_SIZE requests are allowed through, then every caller blocks until
# RATE_LIMIT_COOLDOWN_SECONDS has passed before the next batch is allowed.
RATE_LIMIT_BATCH_SIZE = int(os.environ.get("MASSIVE_RATE_LIMIT_BATCH_SIZE", "10"))
RATE_LIMIT_COOLDOWN_SECONDS = float(os.environ.get("MASSIVE_RATE_LIMIT_COOLDOWN_SECONDS", "1.0"))

# A second, much longer tier on top of the batch throttle above: independent of the
# batch/cooldown cycle, every RATE_LIMIT_LONG_COOLDOWN_REQUESTS requests (lifetime total,
# never resets) forces an additional RATE_LIMIT_LONG_COOLDOWN_SECONDS pause. Meant to back
# a long-running job off periodically over its full run, not just within each short batch.
RATE_LIMIT_LONG_COOLDOWN_REQUESTS = int(os.environ.get("MASSIVE_RATE_LIMIT_LONG_COOLDOWN_REQUESTS", "1000"))
RATE_LIMIT_LONG_COOLDOWN_SECONDS = float(os.environ.get("MASSIVE_RATE_LIMIT_LONG_COOLDOWN_SECONDS", "5.0"))

# The two tiers above cap steady-state throughput but don't react to massive.com telling
# us we're already over its limit - each 429 was only ever handled by the one client that
# received it (see _request's retry loop), so every other thread kept sending at full
# speed regardless. This makes a 429 from *any* request trip a shared cooldown that every
# thread's next acquire() call has to wait out, so the whole process actually backs off.
RATE_LIMIT_429_COOLDOWN_SECONDS = float(os.environ.get("MASSIVE_RATE_LIMIT_429_COOLDOWN_SECONDS", "30.0"))


class GlobalRateLimiter:
    """Process-wide gate: lets `batch_size` requests through, then blocks every caller
    until `cooldown_seconds` has passed before letting the next batch through.

    Uses a threading.Lock/time.sleep rather than an asyncio primitive because sync jobs
    run DataClient instances across multiple OS threads, each with its own event loop
    (see jobs/sync_bars.py's client_factory pattern) - an asyncio.Lock or asyncio.sleep
    can't be shared safely across different threads' event loops, but a plain lock can.
    """

    def __init__(self, batch_size: int, cooldown_seconds: float) -> None:
        self._batch_size = batch_size
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._count = 0
        self._batch_full_at: float | None = None

    def acquire(self) -> None:
        while True:
            wait = 0.0
            with self._lock:
                if self._count >= self._batch_size:
                    elapsed = monotonic() - self._batch_full_at
                    if elapsed >= self._cooldown_seconds:
                        self._count = 0
                        self._batch_full_at = None
                    else:
                        wait = self._cooldown_seconds - elapsed
                if wait == 0.0:
                    self._count += 1
                    if self._count == self._batch_size:
                        self._batch_full_at = monotonic()
                    return
            sleep(wait)


class Global429CoolDown:
    """Process-wide gate that's normally open. Any caller can trip() it - typically after
    a 429 response - which blocks every subsequent acquire() call (including ones already
    in flight on other threads) until `cooldown_seconds` has passed since the trip.

    Same threading.Lock/time.sleep approach as GlobalRateLimiter, for the same reason:
    shared across threads, each with its own event loop, so no asyncio primitive works.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._blocked_until: float | None = None

    def trip(self) -> None:
        with self._lock:
            self._blocked_until = monotonic() + self._cooldown_seconds

    def acquire(self) -> None:
        while True:
            wait = 0.0
            with self._lock:
                if self._blocked_until is not None:
                    remaining = self._blocked_until - monotonic()
                    if remaining <= 0:
                        self._blocked_until = None
                    else:
                        wait = remaining
                if wait == 0.0:
                    return
            sleep(wait)


class ChainedRateLimiter:
    """Gates a request through multiple rate-limiter tiers in sequence (each exposing an
    `acquire()` that blocks as needed) - e.g. a short "x requests per y seconds" batch
    tier, a much longer "extra pause every N requests" tier, and a 429 cooldown that any
    of them can trip - so all of them apply on every request."""

    def __init__(self, *tiers: GlobalRateLimiter | Global429CoolDown) -> None:
        self._tiers = tiers

    def acquire(self) -> None:
        for tier in self._tiers:
            tier.acquire()


_global_429_cooldown = Global429CoolDown(RATE_LIMIT_429_COOLDOWN_SECONDS)

_global_rate_limiter = ChainedRateLimiter(
    GlobalRateLimiter(RATE_LIMIT_BATCH_SIZE, RATE_LIMIT_COOLDOWN_SECONDS),
    GlobalRateLimiter(RATE_LIMIT_LONG_COOLDOWN_REQUESTS, RATE_LIMIT_LONG_COOLDOWN_SECONDS),
    _global_429_cooldown,
)


class DataClient:
    """Async HTTP client for fetching data.

    Defaults to https://api.massive.com/ - pass base_url to point at a different
    data source instead.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = os.environ.get("MASSIVE_API_KEY", "") if api_key is None else api_key
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)
        self._last_request_at: float | None = None

    async def __aenter__(self) -> "DataClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        response = await self._request("POST", path, json=json)
        return response.json()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            await asyncio.to_thread(_global_rate_limiter.acquire)
            await self._pace()
            response = await self._client.request(method, path, **kwargs)
            if response.status_code != httpx.codes.TOO_MANY_REQUESTS or attempt == MAX_RETRIES:
                response.raise_for_status()
                return response

            _global_429_cooldown.trip()
            delay = self._retry_after_seconds(response) or backoff
            logger.warning(
                "429 from %s %s, retrying in %.1fs (attempt %d/%d)",
                method,
                path,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        raise AssertionError("unreachable")  # loop always returns or raises

    async def _pace(self) -> None:
        """Blocks until at least MIN_REQUEST_INTERVAL_SECONDS has passed since the last
        request this client sent, so paged callers don't outrun massive.com's limiter."""
        if self._last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL_SECONDS - (monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_at = monotonic()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None
