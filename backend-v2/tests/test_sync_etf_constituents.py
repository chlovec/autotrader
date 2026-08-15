from pathlib import Path

import httpx
import pytest

from jobs import sync_etf_constituents as job_module
from jobs.control import JobCancelled, JobControl
from jobs.sync_etf_constituents import holdings_url, sync_etf_constituents


class _MockAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient with its transport swapped for a MockTransport - used to patch
    jobs.sync_etf_constituents.httpx.AsyncClient wholesale, since sync_etf_constituents
    constructs its own client internally rather than accepting one as an argument."""

    def __init__(self, handler, **kwargs):
        kwargs.pop("follow_redirects", None)
        super().__init__(transport=httpx.MockTransport(handler), **kwargs)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(job_module.httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs))


@pytest.fixture(autouse=True)
def _download_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "etf_holdings"
    monkeypatch.setattr(job_module, "DOWNLOAD_DIR", directory)
    return directory


async def test_downloads_each_ticker_to_its_own_file(monkeypatch, _download_dir):
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        ticker = str(request.url).rsplit("holdings-daily-us-en-", 1)[1].removesuffix(".xlsx")
        return httpx.Response(200, content=f"fake xlsx bytes for {ticker}".encode())

    _patch_client(monkeypatch, handler)

    downloaded = await sync_etf_constituents(["SPY", "XLK"])

    assert downloaded == 2
    assert (_download_dir / "spy.xlsx").read_bytes() == b"fake xlsx bytes for spy"
    assert (_download_dir / "xlk.xlsx").read_bytes() == b"fake xlsx bytes for xlk"
    assert requested_urls == [holdings_url("SPY"), holdings_url("XLK")]


async def test_lowercases_ticker_in_url_and_filename(monkeypatch, _download_dir):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, content=b"data"))

    await sync_etf_constituents(["spy"])

    assert (_download_dir / "spy.xlsx").exists()


async def test_per_ticker_failure_is_skipped_not_fatal(monkeypatch, _download_dir):
    def handler(request: httpx.Request) -> httpx.Response:
        if "notaspdr" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"data")

    _patch_client(monkeypatch, handler)

    downloaded = await sync_etf_constituents(["SPY", "NOTASPDR"])

    assert downloaded == 1
    assert (_download_dir / "spy.xlsx").exists()
    assert not (_download_dir / "notaspdr.xlsx").exists()


async def test_rerun_overwrites_previous_file(monkeypatch, _download_dir):
    _download_dir.mkdir(parents=True)
    (_download_dir / "spy.xlsx").write_bytes(b"stale data")
    _patch_client(monkeypatch, lambda request: httpx.Response(200, content=b"fresh data"))

    await sync_etf_constituents(["SPY"])

    assert (_download_dir / "spy.xlsx").read_bytes() == b"fresh data"


async def test_empty_ticker_list_downloads_nothing(monkeypatch, _download_dir):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, content=b"data"))

    downloaded = await sync_etf_constituents([])

    assert downloaded == 0
    assert not _download_dir.exists()


async def test_cancel_requested_before_run_raises_without_downloading(monkeypatch, _download_dir):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, content=b"data"))
    control = JobControl()
    control.request_cancel()

    with pytest.raises(JobCancelled):
        await sync_etf_constituents(["SPY"], control=control)

    assert not (_download_dir / "spy.xlsx").exists()
