"""Screens the stored stock/ETF universe (db.models.UniverseSymbol, kept in sync by
engine/universe_sync.py) and persists the results (db.models.ResearchResult) for
engine/multi_runner.py's signal-strategy accounts to trade from - see
db.queries.get_watchlist_symbols.

Scheduling lives in backend/app/main.py, not here: it runs research_once() nightly via
its own BackgroundScheduler (gated by the dashboard's ResearchSchedule toggle) and exposes
a manual-trigger endpoint, since the dashboard's toggle/button need to take effect
immediately and the backend is the one process guaranteed to be running whenever the
dashboard is in use. main() below is a one-shot entrypoint for run_research.py - useful for
a manual run, or OS-level cron on a box that doesn't run the backend persistently - not a
second scheduler.

Research is global (one screen shared by every signal-strategy account, not scoped to any
one account - see ARCHITECTURE.md), but get_bars still needs *a* live broker connection.
research_once takes one in rather than building it, since with multiple accounts there's no
single "the broker" anymore - both the backend and run_research.py's main() resolve it as
the first active account's broker, sorted by id (engine.accounts.get_active_accounts).

The universe (commonly thousands of symbols once filtered to tradable/major-exchange, see
engine/universe_sync.py) is screened in fixed-size batches rather than all at once: one run
still produces one ResearchResult.run_at batch (every row across every sub-batch shares the
same timestamp - see ResearchResult's docstring for why that invariant matters), but bars/
news are fetched and results committed batch-by-batch so a long run's progress survives an
interruption instead of being held in memory until the very end.
"""

import datetime as dt
import logging
import time

from sqlalchemy import select, update

from db.models import ResearchResult
from db.queries import get_research_universe_symbols
from db.session import get_session, init_db
from engine.accounts import get_research_account, sync_accounts_from_env
from engine.brokers import make_broker
from engine.brokers.base import BrokerClient, Timeframe
from engine.config import Config, load_account_credentials, load_config
from engine.notifications import log_and_notify, make_notifier
from engine.research import combine, fetch_universe_news, make_news_client, score_news, score_technical
from engine.universe_sync import ensure_universe_fresh

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.research_runner")

BATCH_SIZE = 30

# Keeps sequential get_bars calls safe under IBKR's historical-data pacing limits (rapid-fire
# reqHistoricalData calls trigger pacing violations); a harmless no-op cost against Alpaca/Questrade.
_INTER_SYMBOL_DELAY_SECONDS = 0.2


def research_once(
    broker: BrokerClient, selected_count: int, technical_weight: float = 0.5, news_weight: float = 0.5,
    config: Config | None = None, batch_size: int = BATCH_SIZE,
) -> None:
    config = config or load_config(argv=[])
    news_client = make_news_client(config)
    notifier = make_notifier(config)

    with get_session() as session:
        ensure_universe_fresh(session, config)
        universe = get_research_universe_symbols(session)

    if not universe:
        logger.info("research run skipped: universe is empty (no tradable symbols synced yet)")
        return

    run_at = dt.datetime.utcnow()  # generated once, shared by every row across every batch - see ResearchResult docstring
    total_scored = 0
    all_skipped: list[str] = []

    for batch_start in range(0, len(universe), batch_size):
        batch = universe[batch_start : batch_start + batch_size]
        news_by_symbol = fetch_universe_news(news_client, batch)

        batch_rows: list[ResearchResult] = []
        for i, symbol in enumerate(batch):
            if batch_start > 0 or i > 0:
                time.sleep(_INTER_SYMBOL_DELAY_SECONDS)
            try:
                bars = broker.get_bars(symbol, Timeframe.DAY, limit=100)
                technical_score, technical_reason = score_technical(bars)
            except Exception:
                logger.exception("failed to fetch/score bars for %s, skipping", symbol)
                all_skipped.append(symbol)
                continue

            news_score, news_reason = score_news(news_by_symbol.get(symbol, []))
            combined_score = combine(technical_score, news_score, technical_weight, news_weight)
            batch_rows.append(
                ResearchResult(
                    run_at=run_at,
                    symbol=symbol,
                    technical_score=technical_score,
                    news_score=news_score,
                    combined_score=combined_score,
                    rationale=f"technical: {technical_reason} | news: {news_reason}",
                )
            )

        with get_session() as session:
            session.add_all(batch_rows)
            session.commit()
        total_scored += len(batch_rows)

    with get_session() as session:
        top_ids = session.execute(
            select(ResearchResult.id)
            .where(ResearchResult.run_at == run_at)
            .order_by(ResearchResult.combined_score.desc())
            .limit(selected_count)
        ).scalars().all()
        if top_ids:
            session.execute(update(ResearchResult).where(ResearchResult.id.in_(top_ids)).values(selected=True))
            session.commit()

        selected_symbols = session.execute(
            select(ResearchResult.symbol)
            .where(ResearchResult.run_at == run_at, ResearchResult.selected.is_(True))
            .order_by(ResearchResult.combined_score.desc())
        ).scalars().all()
        summary = (
            f"research run complete: {total_scored} scored, {len(all_skipped)} skipped, "
            f"selected: {', '.join(selected_symbols) or 'none'}"
        )
        if all_skipped:
            summary += f" (skipped: {', '.join(all_skipped)})"
        log_and_notify(session, notifier, "info", "research_runner", summary)

    logger.info(summary)


def main(
    selected_count: int,
    technical_weight: float = 0.5,
    news_weight: float = 0.5,
    config: Config | None = None,
) -> None:
    """One-shot manual/CLI entrypoint - python run_research.py, or an OS-level cron on a
    box that doesn't run the backend persistently. For the automatic nightly schedule tied
    to the dashboard's toggle and "Run research now" button, run the backend
    (backend/app/main.py owns that scheduler) - don't run both unattended against the same
    database, since neither is aware of the other's runs."""
    config = config or load_config(argv=[])
    init_db()
    with get_session() as session:
        sync_accounts_from_env(session, config)
        account = get_research_account(session)
    if account is None:
        raise RuntimeError("no active accounts configured (ACCOUNT_IDS) - research needs at least one active account's broker connection")

    broker = make_broker(account.id, load_account_credentials(account.id))
    research_once(broker, selected_count, technical_weight, news_weight, config=config)
