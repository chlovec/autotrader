# Design 1 — Swing Trading Scanner (IBKR)

Holding period: days to a few weeks. Scans run on a schedule (not streaming),
propose candidates, and wait for human approval before anything touches an
IBKR order.

## 1. Scope & Goals

- Surface a short, ranked list of swing candidates each trading day.
- Combine technical setup, fundamental quality, catalyst/news context, and
  VWAP-relative entry timing into one score per symbol.
- Never place an order automatically — every proposal ends in an approval
  step before an `create_order_instruction` (or equivalent) is created.
- Fully independent of the existing `backend`/`backend-v2` autotrader stack:
  own process, own database, own job scheduler. No shared code or shared
  SQLite files.

## 2. Scan Cadence

- **Primary scan**: once per day, after market close (using that day's final
  bar). This is the cheapest, most reliable cadence — daily bars are stable
  and don't require streaming infrastructure.
- **Optional secondary scan**: a lightweight re-check ~30–60 minutes after
  the open, to confirm gap behavior and VWAP positioning on names that
  triggered the EOD scan, before the approval digest goes out.
- No intraday polling loop beyond that — this is a swing scanner, not a day
  trading scanner (see Design 2 for that cadence).

## 3. Universe (configurable per run)

Two supported modes, selected per scan config — not hardcoded:

1. **Custom watchlist** — a user-curated list of symbols, mirrored to/from
   an IBKR watchlist via `create_watchlist` / `edit_watchlist` /
   `get_watchlist(s)`. The scanner treats this as its candidate pool and
   only asks "which of these, and when."
2. **Sector/theme subset** — pull constituents from `get_company_themes`
   and `get_theme_details` (or `search_investment_topics` for a named
   theme), bounding the universe to a coherent thematic set (e.g.
   "semiconductors", "GLP-1 drugs") rather than scanning the whole market.

Both modes feed the same scoring pipeline below; the only difference is
where the candidate list comes from.

## 4. Data & IBKR Integration

| Need | Source |
|---|---|
| Daily OHLCV history (for SMA/EMA/RSI/ATR/rel-volume) | `get_price_history` |
| Current/last price, day range | `get_price_snapshot` |
| Intraday VWAP anchor (for entry timing) | `get_price_history` at finer bar size for the scan day, or `get_price_snapshot` if it exposes VWAP directly — needs a spike to confirm the field is available at this granularity |
| Fundamentals (growth, margins, valuation) | No dedicated fundamentals-data tool is currently visible in the IBKR MCP surface — this is a gap. `backend-v2` already integrates a Polygon-schema-compatible provider (`massive.com`) for bars/reference/snapshots/news with proven auth and rate-limiting, but doesn't currently call a financials endpoint either — worth checking whether one exists on that provider before standing up a new vendor or falling back to native `reqFundamentalData` (see Design 3, §4/§9, where this is the bigger blocker). |
| Sector/theme context | `get_company_themes`, `get_theme_details`, `get_company_connections` |
| News/catalyst | `search_investment_topics`, `whats_new` as available; may also need a dedicated news/earnings-calendar feed if IBKR's coverage is too thin for catalyst detection |
| Watchlist sync | `create_watchlist`, `edit_watchlist`, `get_watchlist(s)`, `delete_watchlist` |
| Price alerts on approved candidates | `create_alert`, `get_alerts`, `update_alert`, `set_alert_status` |
| Proposed trades | `create_order_instruction` (proposal only — see §6) |
| Portfolio context for sizing | `get_account_balances`, `get_account_positions` |

Rate limits matter less here than in the day-trading design since this is a
once-a-day batch job over a bounded universe (tens to low hundreds of
symbols), not a continuous scan of the whole market.

## 5. Screening & Scoring Pipeline

Four signal families, each user-selected as required inputs:

1. **Technical** — trend (price vs 20/50/200 SMA), momentum (RSI, MACD),
   volatility (ATR for stop/target sizing), relative volume vs 20-day
   average, breakout/base patterns (e.g. price clearing a multi-week
   consolidation high).
2. **Fundamentals** — quality/growth screen (revenue & earnings growth
   trend, margin trend, debt levels) used as a *gate*, not a daily-moving
   score — refreshed weekly, not daily, since fundamentals don't change
   intraday.
3. **News/catalyst** — recent earnings beat/miss, guidance change, analyst
   rating change, thematic tailwind from the sector/theme data. Acts as a
   score booster and as context shown to the user in the digest (the "why
   now").
4. **VWAP** — used specifically for *entry timing*, not candidate discovery:
   is the stock reclaiming VWAP from below (bullish entry context) or
   extended well above it (chase risk)? This becomes a filter/flag on
   otherwise-qualified technical candidates, not a standalone screen.

Composite score = weighted blend of (1) and (3), gated by (2), annotated
with (4). Weights should be configurable, not fixed — swing strategies vary
in how much they lean on momentum vs quality.

## 6. Approval-Gated Output

1. End-of-day scan produces a ranked shortlist (e.g. top 10–20) with the
   score breakdown and a one-line "why" per candidate.
2. A digest is generated (report file, or pushed via whatever notification
   channel the environment supports) — no IBKR side effects yet.
3. User reviews and approves/rejects individual candidates.
4. For each approved candidate:
   - Optionally added/kept on the relevant IBKR watchlist.
   - A price alert created via `create_alert` at the intended entry level
     (e.g. VWAP reclaim, breakout trigger price).
   - A proposed order recorded via `create_order_instruction` with
     suggested entry, stop (ATR-based), and target — sized against
     `get_account_balances` / `get_account_positions` so proposals respect
     current buying power and existing exposure.
5. Nothing executes without the user acting on the order instruction /
   alert themselves. The scanner's job ends at "proposal recorded."

## 7. Architecture Components

- **Scheduler**: simple daily cron-style trigger (own lightweight job
  runner, not reusing `backend-v2/jobs/*` — kept independent per the scope
  above). One job: `daily_swing_scan`.
- **Universe resolver**: reads scan config (watchlist vs theme), resolves
  to a symbol list for that run.
- **Data ingestion layer**: pulls price history/snapshots/fundamentals/news
  for the resolved universe, writes to the scanner's own SQLite DB (e.g.
  `scanner_swing.db` — a new file, never `autotrader.db` or
  `backend_v2.db`).
- **Scoring engine**: computes the four signal families + composite score,
  persists per-run results for later review/backtesting of the scanner
  itself.
- **Digest generator**: renders the day's shortlist for review.
- **Approval queue**: persisted table of proposed candidates and their
  approve/reject/expire state.
- **Action layer**: on approval, calls the IBKR MCP tools in §6 (watchlist,
  alert, order instruction). This is the only layer allowed to touch IBKR
  write endpoints, and only after an explicit approval record exists.

## 8. Risk Controls

- Stop-loss and position size are always suggested using ATR and account
  balance — never a flat/arbitrary size.
- Fundamentals act as a hard gate (e.g. no proposals on names failing a
  minimum quality bar), not just a score component, to avoid chasing pure
  momentum on weak businesses.
- Approval queue entries expire after N days if not acted on (a swing setup
  proposed last week isn't valid today).

## 9. Open Questions

- Confirm whether IBKR fundamentals data is reachable at all through the
  available MCP tools, or whether a separate fundamentals feed is required.
- Confirm VWAP availability/granularity from `get_price_history` /
  `get_price_snapshot` for the "entry timing" filter in §5.
- Decide notification channel for the daily digest (email, Slack, local
  file + CLI prompt, etc.) — not an IBKR concern, but blocks §6 until
  chosen.
