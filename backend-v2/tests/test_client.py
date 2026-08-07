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


async def test_retries_on_429_then_succeeds(monkeypatch):
    import data.client as client_module

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"ok": True})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        result = await client.get("/v1/quotes")

    assert result == {"ok": True}
    assert calls["count"] == 3
    assert sleeps == [2.0, 2.0]


async def test_gives_up_after_max_retries_on_429(monkeypatch):
    import data.client as client_module

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module, "MAX_RETRIES", 2)
    monkeypatch.setattr(client_module, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/v1/quotes")

    assert calls["count"] == 3


async def test_paces_requests_at_least_min_interval_apart(monkeypatch):
    import data.client as client_module

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module, "MIN_REQUEST_INTERVAL_SECONDS", 5.0)

    # 1st _pace(): no prior timestamp, just records 100.0. 2nd _pace(): checks elapsed
    # (100.1 - 100.0 = 0.1s < 5.0s min) then records the post-wait timestamp (100.1).
    times = iter([100.0, 100.1, 100.1])
    monkeypatch.setattr(client_module, "monotonic", lambda: next(times))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        await client.get("/v1/quotes")
        await client.get("/v1/quotes")

    assert sleeps == [pytest.approx(4.9)]
