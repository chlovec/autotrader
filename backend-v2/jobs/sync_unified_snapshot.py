"""Syncs GET /v3/snapshot ("Unified Snapshot") into the unified_snapshots table, one
asset-class `type` at a time (stocks/options/indices/fx/crypto - see
jobs/registry.py's SNAPSHOT_TYPE_OPTIONS).

Unlike every other sync_* job here, this one isn't ticker-driven at all - massive.com
doesn't expose a "give me everything" mode without a `type` filter, so a run walks
every selected type's full paged result set (next_url paging, same shape as
jobs/sync_bars.py's fetch_and_store_bars) and upserts every ticker it returns. See
db/models.py's UnifiedSnapshot for why one table with a plain ticker primary key covers
all five asset classes without needing a second key column the way
db/models.py's TopMarketMover needs `direction`.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import UnifiedSnapshot
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.registry import SNAPSHOT_TYPE_OPTIONS

logger = logging.getLogger("backend_v2.jobs.sync_unified_snapshot")

JOB_NAME = "unified_snapshot"
UNIFIED_SNAPSHOT_PATH = "/v3/snapshot"
PAGE_LIMIT = 250


def _to_datetime(value: int | float | None, divisor: float = 1_000_000_000) -> dt.datetime | None:
    """massive.com reports every *_updated/*_timestamp field here as epoch
    nanoseconds - same conversion as jobs/sync_snapshots.py's _to_datetime, just with
    that one fixed divisor since every timestamp field on this endpoint uses it."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / divisor, tz=dt.timezone.utc).replace(tzinfo=None)


def _to_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    return dt.date.fromisoformat(value)


def _values_for(result: dict[str, Any]) -> dict[str, Any]:
    session_block = result.get("session") or {}
    last_quote = result.get("last_quote") or {}
    last_trade = result.get("last_trade") or {}
    details = result.get("details") or {}
    greeks = result.get("greeks") or {}
    underlying = result.get("underlying_asset") or {}
    conditions = last_trade.get("conditions")

    return {
        "ticker": result.get("ticker"),
        "type": result.get("type"),
        "name": result.get("name"),
        "market_status": result.get("market_status"),
        "error": result.get("error"),
        "message": result.get("message"),
        "value": result.get("value"),
        "fmv": result.get("fmv"),
        "break_even_price": result.get("break_even_price"),
        "implied_volatility": result.get("implied_volatility"),
        "open_interest": result.get("open_interest"),
        "session_change": session_block.get("change"),
        "session_change_percent": session_block.get("change_percent"),
        "session_early_trading_change": session_block.get("early_trading_change"),
        "session_early_trading_change_percent": session_block.get("early_trading_change_percent"),
        "session_late_trading_change": session_block.get("late_trading_change"),
        "session_late_trading_change_percent": session_block.get("late_trading_change_percent"),
        "session_close": session_block.get("close"),
        "session_high": session_block.get("high"),
        "session_low": session_block.get("low"),
        "session_open": session_block.get("open"),
        "session_previous_close": session_block.get("previous_close"),
        "session_volume": session_block.get("volume"),
        "last_quote_ask": last_quote.get("ask"),
        "last_quote_ask_size": last_quote.get("ask_size"),
        "last_quote_ask_exchange": last_quote.get("ask_exchange"),
        "last_quote_bid": last_quote.get("bid"),
        "last_quote_bid_size": last_quote.get("bid_size"),
        "last_quote_bid_exchange": last_quote.get("bid_exchange"),
        "last_quote_exchange": last_quote.get("exchange"),
        "last_quote_midpoint": last_quote.get("midpoint"),
        "last_quote_timeframe": last_quote.get("timeframe"),
        "last_quote_last_updated": _to_datetime(last_quote.get("last_updated")),
        "last_trade_conditions": ",".join(str(code) for code in conditions) if conditions else None,
        "last_trade_exchange": last_trade.get("exchange"),
        "last_trade_id": last_trade.get("id"),
        "last_trade_price": last_trade.get("price"),
        "last_trade_size": last_trade.get("size"),
        "last_trade_timeframe": last_trade.get("timeframe"),
        "last_trade_sip_timestamp": _to_datetime(last_trade.get("sip_timestamp")),
        "details_contract_type": details.get("contract_type"),
        "details_exercise_style": details.get("exercise_style"),
        "details_expiration_date": _to_date(details.get("expiration_date")),
        "details_shares_per_contract": details.get("shares_per_contract"),
        "details_strike_price": details.get("strike_price"),
        "details_ticker": details.get("ticker"),
        "greeks_delta": greeks.get("delta"),
        "greeks_gamma": greeks.get("gamma"),
        "greeks_theta": greeks.get("theta"),
        "greeks_vega": greeks.get("vega"),
        "underlying_asset_change_to_break_even": underlying.get("change_to_break_even"),
        "underlying_asset_last_updated": _to_datetime(underlying.get("last_updated")),
        "underlying_asset_price": underlying.get("price"),
        "underlying_asset_ticker": underlying.get("ticker"),
        "underlying_asset_timeframe": underlying.get("timeframe"),
        "underlying_asset_value": underlying.get("value"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    }


def _upsert_unified_snapshot(session: Session, result: dict[str, Any]) -> None:
    values = _values_for(result)
    if not values["ticker"]:
        return
    stmt = sqlite_insert(UnifiedSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[UnifiedSnapshot.ticker], set_=values)
    session.execute(stmt)


async def _fetch_type(
    client: DataClient, session: Session, snapshot_type: str, control: JobControl | None = None
) -> int:
    """Pages through one asset-class type's full result set (next_url paging, same
    shape as jobs/sync_bars.py's fetch_and_store_bars) and upserts every ticker."""
    params: dict[str, Any] = {"type": snapshot_type, "limit": PAGE_LIMIT}
    next_url: str | None = None
    fetched = 0
    while True:
        if control is not None:
            await control.checkpoint_async()
        payload = await (client.get(next_url) if next_url else client.get(UNIFIED_SNAPSHOT_PATH, params=params))
        results = payload.get("results") or []
        for result in results:
            _upsert_unified_snapshot(session, result)
        fetched += len(results)
        session.commit()

        next_url = payload.get("next_url")
        if not next_url:
            break
    return fetched


async def sync_unified_snapshot(
    client: DataClient,
    session: Session,
    snapshot_types: list[str] | None = None,
    control: JobControl | None = None,
) -> dict[str, int]:
    """Fetches and upserts every ticker for each selected asset-class type - every
    type in SNAPSHOT_TYPE_OPTIONS if none is given. One type's failure doesn't stop
    the rest, same per-item isolation as sync_bars_manual; a cancel (JobCancelled) is
    left uncaught so it unwinds the whole run instead."""
    types = snapshot_types or SNAPSHOT_TYPE_OPTIONS
    results: dict[str, int] = {}
    for snapshot_type in types:
        try:
            results[snapshot_type] = await _fetch_type(client, session, snapshot_type, control)
        except JobCancelled:
            raise
        except Exception:
            logger.exception("unified snapshot sync failed for type %s", snapshot_type)

    logger.info("synced unified snapshot: %s", ", ".join(f"{t}={n}" for t, n in results.items()))
    return results


async def run_once(snapshot_types: list[str] | None = None) -> dict[str, int]:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_unified_snapshot(client, session, snapshot_types)
        finally:
            session.close()
