import httpx
import pytest

from data.client import ChainedRateLimiter, DataClient, Global429CoolDown, GlobalRateLimiter


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
    # Isolated instances: a 429 here would otherwise trip the process-wide 429 cooldown
    # singleton and block every later test's real requests for up to 30 real seconds.
    monkeypatch.setattr(client_module, "_global_rate_limiter", GlobalRateLimiter(1000, 0.0))
    monkeypatch.setattr(client_module, "_global_429_cooldown", Global429CoolDown(0.0))

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
    # Isolated instances: a 429 here would otherwise trip the process-wide 429 cooldown
    # singleton and block every later test's real requests for up to 30 real seconds.
    monkeypatch.setattr(client_module, "_global_rate_limiter", GlobalRateLimiter(1000, 0.0))
    monkeypatch.setattr(client_module, "_global_429_cooldown", Global429CoolDown(0.0))

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
    # Isolated instance so this test doesn't trip the global batch gate.
    monkeypatch.setattr(client_module, "_global_rate_limiter", GlobalRateLimiter(1000, 0.0))

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


async def test_global_rate_limiter_allows_batch_size_without_waiting():
    limiter = GlobalRateLimiter(batch_size=3, cooldown_seconds=1.0)

    # First `batch_size` acquisitions should return immediately (no sleep patched in, so
    # any blocking call here would hang/fail the test on its own).
    for _ in range(3):
        limiter.acquire()

    assert limiter._count == 3
    assert limiter._batch_full_at is not None


def test_global_rate_limiter_blocks_until_cooldown_elapses(monkeypatch):
    import data.client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "sleep", sleeps.append)

    # Call 1: count 0->1, no monotonic call. Call 2: count 1->2, records batch_full_at
    # (100.0). Call 3: elapsed=0 -> sleeps 1.0, re-checks (elapsed=0.2 -> sleeps 0.8),
    # re-checks again (elapsed=1.0, >= cooldown) -> batch resets and the 3rd is let in.
    times = iter([100.0, 100.0, 100.2, 101.0])
    monkeypatch.setattr(client_module, "monotonic", lambda: next(times))

    limiter = GlobalRateLimiter(batch_size=2, cooldown_seconds=1.0)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [pytest.approx(1.0), pytest.approx(0.8)]
    assert limiter._count == 1


def test_chained_rate_limiter_acquires_every_tier_in_order():
    calls: list[str] = []

    class FakeTier:
        def __init__(self, name: str) -> None:
            self._name = name

        def acquire(self) -> None:
            calls.append(self._name)

    limiter = ChainedRateLimiter(FakeTier("short"), FakeTier("long"))
    limiter.acquire()

    assert calls == ["short", "long"]


def test_chained_rate_limiter_long_tier_gates_every_n_requests(monkeypatch):
    import data.client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "sleep", sleeps.append)

    # short tier's batch_size (1000) is never reached, so it makes no monotonic() calls.
    # long tier: call 2 fills its batch (batch_full_at=100.0); call 3's first check sees
    # elapsed=0 -> sleeps 5.0, its second check sees elapsed=5.0 (>= cooldown) -> resets.
    times = iter([100.0, 100.0, 105.0])
    monkeypatch.setattr(client_module, "monotonic", lambda: next(times))

    limiter = ChainedRateLimiter(
        GlobalRateLimiter(batch_size=1000, cooldown_seconds=0.0),
        GlobalRateLimiter(batch_size=2, cooldown_seconds=5.0),
    )
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [pytest.approx(5.0)]


def test_global_429_cooldown_acquire_is_a_noop_before_any_trip():
    cooldown = Global429CoolDown(cooldown_seconds=30.0)

    # No trip() call, so acquire() must return immediately - any blocking call here would
    # hang/fail the test on its own (no sleep patched in).
    cooldown.acquire()

    assert cooldown._blocked_until is None


def test_global_429_cooldown_blocks_every_caller_until_cooldown_elapses(monkeypatch):
    import data.client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "sleep", sleeps.append)

    # trip() records blocked_until = 100.0 + 30.0 = 130.0. acquire()'s first check sees
    # remaining=30.0 -> sleeps 30.0; its second check (at 130.0) sees remaining<=0 -> clears.
    times = iter([100.0, 100.0, 130.0])
    monkeypatch.setattr(client_module, "monotonic", lambda: next(times))

    cooldown = Global429CoolDown(cooldown_seconds=30.0)
    cooldown.trip()
    cooldown.acquire()

    assert sleeps == [pytest.approx(30.0)]
    assert cooldown._blocked_until is None


async def test_data_client_trips_global_429_cooldown_on_429_response(monkeypatch):
    import data.client as client_module

    class FakeCooldown:
        def __init__(self) -> None:
            self.trip_calls = 0

        def trip(self) -> None:
            self.trip_calls += 1

        def acquire(self) -> None:
            pass

    fake_cooldown = FakeCooldown()

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(client_module, "_global_rate_limiter", GlobalRateLimiter(1000, 0.0))
    monkeypatch.setattr(client_module, "_global_429_cooldown", fake_cooldown)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"ok": True})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        await client.get("/v1/quotes")

    assert fake_cooldown.trip_calls == 1


async def test_data_client_acquires_global_rate_limiter_per_request(monkeypatch):
    import data.client as client_module

    acquire_calls = {"count": 0}
    limiter = client_module._global_rate_limiter
    monkeypatch.setattr(limiter, "acquire", lambda: acquire_calls.__setitem__("count", acquire_calls["count"] + 1))
    monkeypatch.setattr(client_module, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client_with_transport(httpx.MockTransport(handler)) as client:
        await client.get("/v1/quotes")
        await client.get("/v1/quotes")

    assert acquire_calls["count"] == 2
