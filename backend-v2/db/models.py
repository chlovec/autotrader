"""backend-v2's own schema - a separate database from v1's db/models.py at the repo root."""

import datetime as dt

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ticker(Base):
    """One row per ticker returned by GET /v3/reference/tickers, keyed by the ticker
    symbol itself. Upserted by jobs/sync_tickers.py - a re-fetched ticker overwrites
    the existing row rather than creating a new one."""

    __tablename__ = "tickers"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    market: Mapped[str | None] = mapped_column(String)
    locale: Mapped[str | None] = mapped_column(String)
    primary_exchange: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool | None] = mapped_column()
    currency_name: Mapped[str | None] = mapped_column(String)
    cik: Mapped[str | None] = mapped_column(String)
    composite_figi: Mapped[str | None] = mapped_column(String)
    share_class_figi: Mapped[str | None] = mapped_column(String)
    last_updated_utc: Mapped[str | None] = mapped_column(String)


class SyncState(Base):
    """One row per job name, tracking when that job last completed successfully so the
    next run can ask the upstream API for only what changed since then. See
    jobs/sync_tickers.py's sync_tickers for how the tickers job uses this.

    last_synced_at is stored naive but is always UTC - sqlite drops tzinfo on
    round-trip, so keeping an explicit timezone on the column would just mean an aware
    datetime went in and a naive one came back out."""

    __tablename__ = "sync_state"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    last_synced_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class OhlcBar(Base):
    """One row per bar returned by GET /v2/aggs/ticker/{ticker}/range/{multiplier}/
    {timespan}/{from}/{to}, keyed by (ticker, multiplier, timespan, timestamp) so the
    same ticker can hold bars of more than one granularity (e.g. daily and 5-minute)
    without colliding. Upserted by jobs/sync_bars.py."""

    __tablename__ = "ohlc_bars"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    multiplier: Mapped[int] = mapped_column(Integer, primary_key=True)
    timespan: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    transactions: Mapped[int | None] = mapped_column(Integer)


class TickerBarSyncState(Base):
    """Per (ticker, multiplier, timespan) cursor: the last date whose bars are known to
    be fully synced. jobs/sync_bars.py's sync_bars_nightly reads this to find tickers
    that aren't up to date and to pick up where each one left off; every successful
    fetch_and_store_bars call advances it (never rewinds it - see that function's
    docstring)."""

    __tablename__ = "ticker_bar_sync_state"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    multiplier: Mapped[int] = mapped_column(Integer, primary_key=True)
    timespan: Mapped[str] = mapped_column(String, primary_key=True)
    synced_through: Mapped[dt.date] = mapped_column(Date)
