"""Downloads each configured ETF's daily holdings workbook from SSGA's public export
and saves the raw file under DOWNLOAD_DIR, overwritten on every run - SSGA's own file
is always "today's holdings," not a dated snapshot, so there's nothing to preserve by
keeping the previous run's copy around.

This only fetches and saves the raw .xlsx - parsing it into structured rows is a later
step (a different job reading these files, once that's designed). The URL pattern below
only resolves for ETFs State Street itself manages (SPY and other SPDR funds), not
arbitrary ETFs, so unlike sync_ticker_types/sync_top_movers this can't just fetch
"everything" - the caller supplies the exact ticker list to attempt (JobConfig.tickers,
same field sync_bars/sync_snapshots use), and job_configs' ticker_types field is not
used at all here, since there's no meaningful "type" to filter SSGA's per-ticker file
URLs by.
"""

import logging
from pathlib import Path

import httpx

from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.sync_etf_constituents")

JOB_NAME = "etf_constituents"

HOLDINGS_URL_TEMPLATE = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-{ticker}.xlsx"
)

# backend-v2/data/etf_holdings/ - sibling to data/client.py, gitignored the same way
# the SQLite databases are (see repo root .gitignore): fetched artifacts, not source.
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "etf_holdings"

# SSGA's CDN rejects requests with no User-Agent outright - a plain desktop-browser UA
# string is enough to get through; there's nothing else browser-specific this needs.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 30.0


def holdings_url(ticker: str) -> str:
    return HOLDINGS_URL_TEMPLATE.format(ticker=ticker.lower())


def _download_path(ticker: str) -> Path:
    return DOWNLOAD_DIR / f"{ticker.lower()}.xlsx"


async def _download_one(client: httpx.AsyncClient, ticker: str) -> bool:
    url = holdings_url(ticker)
    try:
        response = await client.get(url, headers={"User-Agent": _USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("failed to download holdings for %s from %s", ticker, url)
        return False
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _download_path(ticker).write_bytes(response.content)
    return True


async def sync_etf_constituents(tickers: list[str], control: JobControl | None = None) -> int:
    """Downloads holdings-daily-us-en-{ticker}.xlsx for each of `tickers` from SSGA,
    saving the raw bytes to DOWNLOAD_DIR (overwriting whatever was saved for that
    ticker on a previous run). A per-ticker failure - a 404 for a ticker SSGA doesn't
    manage, a transient network error - is logged and skipped rather than aborting the
    whole run, same reasoning as sync_top_movers.py's per-direction try/except.

    `control`, if given, is checked before each ticker (see jobs/control.py) - a pause
    blocks right here until resumed, and a cancel raises JobCancelled, left uncaught so
    it unwinds the whole run instead of being treated as a per-ticker failure.

    Returns the number of files successfully downloaded."""
    downloaded = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for ticker in tickers:
            if control is not None:
                await control.checkpoint_async()
            if await _download_one(client, ticker):
                downloaded += 1
    logger.info("downloaded %d/%d etf holdings file(s)", downloaded, len(tickers))
    return downloaded


async def run_once(tickers: list[str]) -> int:
    return await sync_etf_constituents(tickers)
