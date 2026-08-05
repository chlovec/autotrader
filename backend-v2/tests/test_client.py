import httpx
import pytest

from data.client import DataClient


def _client_with_transport(transport: httpx.MockTransport, **kwargs) -> DataClient:
    client = DataClient(api_key="test-key", **kwargs)
    client._client = httpx.AsyncClient(
        base_url=client._client.base_url,
        headers=client._client.headers,
        transport=transport,
    )
    return client


async def test_get_sends_auth_header_and_returns_json():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        result = await client.get("/v1/quotes", params={"symbol": "AAPL"})

    assert result == {"ok": True}
    request = captured["request"]
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.url.path == "/v1/quotes"
    assert request.url.params["symbol"] == "AAPL"


async def test_default_base_url_is_massive():
    client = DataClient(api_key="test-key")
    assert str(client._client.base_url) == "https://api.massive.com/"
    await client.aclose()


async def test_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/v1/missing")
