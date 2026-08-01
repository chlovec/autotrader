from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import ResearchResult


def get_watchlist_symbols(session: Session, limit: int | None = None) -> list[str]:
    """Symbols selected by the most recent research run, ranked by combined_score desc.
    Returns [] if research has never run."""
    latest_run = session.execute(select(func.max(ResearchResult.run_at))).scalar_one_or_none()
    if latest_run is None:
        return []

    query = (
        select(ResearchResult.symbol)
        .where(ResearchResult.run_at == latest_run, ResearchResult.selected.is_(True))
        .order_by(ResearchResult.combined_score.desc())
    )
    if limit:
        query = query.limit(limit)
    return [row[0] for row in session.execute(query).all()]
