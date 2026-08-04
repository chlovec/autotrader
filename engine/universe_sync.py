"""Keeps db.models.UniverseSymbol - the tradable stock/ETF universe engine/research_runner.py
screens - in sync with Alpaca's asset-listing and snapshot APIs.

Global, not account-scoped, for the same reason engine/research.py's fetch_universe_news
is: this is one shared screen, not something any particular trading account owns. Reuses
Config.alpaca_news_api_key/_secret_key (the existing account-independent Alpaca pair) with
paper=True rather than resolving any account's own broker credentials - a free paper key
works fine for reading public asset/market data, and there's no single "the account" to
default to once multiple accounts (possibly on different brokers) exist.
"""

import datetime as dt
import logging

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import UniverseSymbol
from engine.config import Config

logger = logging.getLogger("autotrader.universe_sync")

# Major listing venues only - excludes OTC/pink-sheet tickers, which tend to be thin,
# low-quality names not worth spending research cycles screening.
_UNIVERSE_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "NYSEARCA", "AMEX"}

# How stale dollar_volume can get before it's worth another round of snapshot calls -
# relative liquidity rank doesn't meaningfully shift day to day the way tradable/name/
# exchange can, so this doesn't need to be refreshed on every sync_universe_assets call.
_LIQUIDITY_REFRESH_INTERVAL = dt.timedelta(days=7)

# Conservative chunk size for get_stock_snapshot - alpaca-py documents no per-request
# symbol cap, so this errs small rather than risking an oversized request.
_SNAPSHOT_CHUNK_SIZE = 200


def make_universe_trading_client(config: Config) -> TradingClient:
    return TradingClient(config.alpaca_news_api_key, config.alpaca_news_secret_key, paper=True)


def make_universe_data_client(config: Config) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.alpaca_news_api_key, config.alpaca_news_secret_key)


def sync_universe_assets(session: Session, trading_client: TradingClient) -> int:
    """Refreshes symbol/name/exchange/tradable metadata for every active US-equity asset,
    one non-paginated get_all_assets() call. Assets Alpaca no longer returns, or returns
    as non-tradable/off a minor exchange, are marked tradable=False here rather than
    deleted - ResearchResult/BlocklistedSymbol reference symbols by plain string, not a
    foreign key, so this never orphans history; the symbol just stops being picked into
    future research batches. Returns the number of tradable symbols after the sync."""
    assets = trading_client.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))

    now = dt.datetime.utcnow()
    seen_symbols: set[str] = set()
    tradable_count = 0
    for asset in assets:
        tradable = bool(asset.tradable) and asset.exchange in _UNIVERSE_EXCHANGES
        seen_symbols.add(asset.symbol)
        tradable_count += tradable

        row = session.get(UniverseSymbol, asset.symbol)
        if row is None:
            row = UniverseSymbol(symbol=asset.symbol)
            session.add(row)
        row.name = asset.name or ""
        row.exchange = asset.exchange.value
        row.tradable = tradable
        row.synced_at = now

    # Anything previously tradable that Alpaca no longer returned at all (delisted/
    # renamed) needs marking too - loop only over currently-tradable rows so this stays
    # cheap as the table grows, rather than scanning every historical row every sync.
    previously_tradable = session.execute(select(UniverseSymbol).where(UniverseSymbol.tradable.is_(True))).scalars().all()
    for row in previously_tradable:
        if row.symbol not in seen_symbols:
            row.tradable = False
            tradable_count -= 1

    session.commit()
    logger.info("universe asset sync: %d tradable symbols", tradable_count)
    return tradable_count


def sync_universe_liquidity(session: Session, data_client: StockHistoricalDataClient) -> None:
    """Refreshes UniverseSymbol.dollar_volume (latest daily volume * close, a market-cap
    stand-in - Alpaca's asset objects carry no such field) via batched snapshot calls.
    Commits per chunk so a failure partway through doesn't lose already-fetched chunks."""
    symbols = session.execute(select(UniverseSymbol.symbol).where(UniverseSymbol.tradable.is_(True))).scalars().all()
    now = dt.datetime.utcnow()

    for i in range(0, len(symbols), _SNAPSHOT_CHUNK_SIZE):
        chunk = symbols[i : i + _SNAPSHOT_CHUNK_SIZE]
        try:
            snapshots = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=chunk))
        except APIError:
            logger.exception("snapshot fetch failed for a chunk of %d symbols, skipping", len(chunk))
            continue

        for symbol, snapshot in snapshots.items():
            daily_bar = snapshot.daily_bar if snapshot else None
            if daily_bar is None:
                continue
            row = session.get(UniverseSymbol, symbol)
            if row is None:
                continue
            row.dollar_volume = daily_bar.volume * daily_bar.close
            row.liquidity_updated_at = now
        session.commit()


def ensure_universe_fresh(session: Session, config: Config) -> None:
    """Called at the start of every research run. Always refreshes asset metadata (cheap,
    one call). Only refreshes liquidity if some tradable symbol has never been snapshotted
    or was last snapshotted over _LIQUIDITY_REFRESH_INTERVAL ago - liquidity rank doesn't
    need to be as fresh as tradability."""
    sync_universe_assets(session, make_universe_trading_client(config))

    stale_cutoff = dt.datetime.utcnow() - _LIQUIDITY_REFRESH_INTERVAL
    has_stale = session.execute(
        select(UniverseSymbol.symbol)
        .where(
            UniverseSymbol.tradable.is_(True),
            (UniverseSymbol.liquidity_updated_at.is_(None)) | (UniverseSymbol.liquidity_updated_at < stale_cutoff),
        )
        .limit(1)
    ).first()
    if has_stale is not None:
        sync_universe_liquidity(session, make_universe_data_client(config))
