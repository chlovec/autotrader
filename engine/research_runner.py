"""Screens a fixed universe of symbols and persists the results (db.models.ResearchResult)
for engine/multi_runner.py's signal-strategy accounts to trade from - see
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
"""

import datetime as dt
import logging
import time

from db.models import ResearchResult
from db.session import get_session, init_db
from engine.accounts import get_research_account, sync_accounts_from_env
from engine.brokers import make_broker
from engine.brokers.base import BrokerClient, Timeframe
from engine.config import Config, load_account_credentials, load_config
from engine.notifications import log_and_notify, make_notifier
from engine.research import combine, fetch_universe_news, make_news_client, score_news, score_technical

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.research_runner")

# Fixed, editable candidate universe - broad large-cap/sector coverage plus a few index
# ETFs. Add/remove tickers here; research_once skips (and logs) any symbol it can't fetch
# bars for, e.g. a delisted/renamed ticker. Shared by run_research.py and
# backend/app/main.py's nightly job, so there's one universe, not two drifting copies.
DEFAULT_UNIVERSE = [
    # mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    # other large caps across sectors
    "JPM", "V", "UNH", "HD", "PG", "JNJ", "XOM", "CVX", "KO", "PEP",
    "WMT", "DIS", "NFLX", "ADBE", "CRM", "AMD", "INTC", "BA", "CAT", "GE",
    # broad index / sector ETFs
    "SPY", "QQQ", "DIA", "IWM", "XLF", "XLK", "XLE", "XLV",
]
DEFAULT_TOP_N = 10

# Keeps sequential get_bars calls safe under IBKR's historical-data pacing limits (rapid-fire
# reqHistoricalData calls trigger pacing violations); a harmless no-op cost against Alpaca/Questrade.
_INTER_SYMBOL_DELAY_SECONDS = 0.2


def research_once(
    universe: list[str], top_n: int, broker: BrokerClient, technical_weight: float = 0.5, news_weight: float = 0.5,
    config: Config | None = None,
) -> None:
    deduped = list(dict.fromkeys(universe))  # preserves order, drops duplicate tickers
    if top_n > len(deduped):
        raise ValueError(f"top_n ({top_n}) exceeds universe size ({len(deduped)})")

    config = config or load_config(argv=[])
    news_client = make_news_client(config)
    notifier = make_notifier(config)

    news_by_symbol = fetch_universe_news(news_client, deduped)
    run_at = dt.datetime.utcnow()  # generated once, shared by every row this run - see ResearchResult docstring

    scored: list[ResearchResult] = []
    skipped: list[str] = []
    for i, symbol in enumerate(deduped):
        if i > 0:
            time.sleep(_INTER_SYMBOL_DELAY_SECONDS)
        try:
            bars = broker.get_bars(symbol, Timeframe.DAY, limit=100)
            technical_score, technical_reason = score_technical(bars)
        except Exception:
            logger.exception("failed to fetch/score bars for %s, skipping", symbol)
            skipped.append(symbol)
            continue

        news_score, news_reason = score_news(news_by_symbol.get(symbol, []))
        combined_score = combine(technical_score, news_score, technical_weight, news_weight)

        scored.append(
            ResearchResult(
                run_at=run_at,
                symbol=symbol,
                technical_score=technical_score,
                news_score=news_score,
                combined_score=combined_score,
                rationale=f"technical: {technical_reason} | news: {news_reason}",
            )
        )

    scored.sort(key=lambda r: r.combined_score, reverse=True)
    for result in scored[:top_n]:
        result.selected = True

    with get_session() as session:
        session.add_all(scored)
        session.commit()

        selected_symbols = [r.symbol for r in scored if r.selected]
        summary = f"research run complete: {len(scored)} scored, {len(skipped)} skipped, selected: {', '.join(selected_symbols) or 'none'}"
        if skipped:
            summary += f" (skipped: {', '.join(skipped)})"
        log_and_notify(session, notifier, "info", "research_runner", summary)

    logger.info(summary)


def main(
    universe: list[str] = DEFAULT_UNIVERSE,
    top_n: int = DEFAULT_TOP_N,
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
    research_once(universe, top_n, broker, technical_weight, news_weight, config=config)
