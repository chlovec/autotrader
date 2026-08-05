"""Manually triggered OHLC bars backfill.

    python run_bars_manual.py --start 2024-01-01 --end 2024-01-31
    python run_bars_manual.py --start 2024-01-01 --end 2024-01-31 --tickers AAPL,MSFT
    python run_bars_manual.py --start 2024-01-01 --end 2024-01-31 --ticker-types CS,ETF

Runs for every ticker in the tickers table if neither --tickers nor --ticker-types is
given. --tickers and --ticker-types are mutually exclusive.
"""

import argparse
import asyncio
import datetime as dt
import logging

from jobs.sync_bars import DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN, run_manual

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend_v2.run_bars_manual")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, type=dt.date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=dt.date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--multiplier", type=int, default=DEFAULT_MULTIPLIER)
    parser.add_argument("--timespan", default=DEFAULT_TIMESPAN, help="e.g. day, minute, hour, week")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--tickers", help="comma-separated ticker symbols, e.g. AAPL,MSFT")
    selector.add_argument("--ticker-types", help="comma-separated ticker types, e.g. CS,ETF")
    return parser.parse_args()


def _split(value: str | None) -> list[str] | None:
    return [item.strip() for item in value.split(",") if item.strip()] if value else None


async def main() -> None:
    args = _parse_args()
    results = await run_manual(
        args.start,
        args.end,
        ticker_types=_split(args.ticker_types),
        tickers=_split(args.tickers),
        multiplier=args.multiplier,
        timespan=args.timespan,
    )
    total = sum(results.values())
    logger.info("synced %d bar(s) across %d ticker(s)", total, len(results))


if __name__ == "__main__":
    asyncio.run(main())
