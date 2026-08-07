import asyncio
import logging
import os
from time import monotonic
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
# every page. No published quota to tune this against - 0.5s (2 req/s) is a conservative
# starting point; tighten it if 429s persist, loosen it if jobs feel unnecessarily slow.
MIN_REQUEST_INTERVAL_SECONDS = 0.005


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
            await self._pace()
            response = await self._client.request(method, path, **kwargs)
            if response.status_code != httpx.codes.TOO_MANY_REQUESTS or attempt == MAX_RETRIES:
                response.raise_for_status()
                return response

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
