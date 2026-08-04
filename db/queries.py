from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import BlocklistedSymbol, ResearchResult, UniverseSymbol


def get_research_universe_symbols(session: Session) -> list[str]:
    """Tradable symbols from the stored universe (see engine/universe_sync.py), ordered by
    `dollar_volume` desc - the liquidity proxy research_once uses to decide which symbols
    to screen first each run. Symbols with no liquidity data yet (never snapshotted) sort
    last rather than first, so an unscored symbol doesn't jump the queue ahead of known-
    liquid ones."""
    query = (
        select(UniverseSymbol.symbol)
        .where(UniverseSymbol.tradable.is_(True))
        .order_by(UniverseSymbol.dollar_volume.desc().nulls_last())
    )
    return [row[0] for row in session.execute(query).all()]


def get_watchlist_symbols(session: Session, limit: int | None = None) -> list[str]:
    """Symbols selected by the most recent research run, ranked by combined_score desc,
    with any user-blocklisted symbol removed. Returns [] if research has never run.

    Blocklist filtering happens in Python after the query rather than as a SQL
    `NOT IN`/join so `limit` still applies to the post-filter, blocklist-aware ranking
    (a SQL-level limit could cut the list down to fewer than `limit` symbols if some of
    the top rows turn out to be blocklisted)."""
    latest_run = session.execute(select(func.max(ResearchResult.run_at))).scalar_one_or_none()
    if latest_run is None:
        return []

    blocked = set(session.execute(select(BlocklistedSymbol.symbol)).scalars().all())

    query = (
        select(ResearchResult.symbol)
        .where(ResearchResult.run_at == latest_run, ResearchResult.selected.is_(True))
        .order_by(ResearchResult.combined_score.desc())
    )
    symbols = [row[0] for row in session.execute(query).all() if row[0] not in blocked]
    return symbols[:limit] if limit else symbols
