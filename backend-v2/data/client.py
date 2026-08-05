import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://api.massive.com/"


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

    async def __aenter__(self) -> "DataClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        response = await self._client.post(path, json=json)
        response.raise_for_status()
        return response.json()
