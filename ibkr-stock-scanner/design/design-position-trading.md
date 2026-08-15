# Design 3 — Position / Long-Term Investing Scanner (IBKR)

Holding period: months and up. Fundamentals-led, low scan frequency,
portfolio-aware — this design cares more about "does this fit the
portfolio and is it a sound business" than about short-term timing.

## 1. Scope & Goals

- Produce a periodic (monthly/quarterly) ranked research list of long-term
  candidates, plus a rebalancing view against the current portfolio.
- Fundamentals dominate candidate discovery; technicals and VWAP are used
  only to sanity-check entry timing, not to find ideas.
- Same hard rule as Designs 1 and 2: proposals only, never autonomous
  execution. Output here is closer to "a research report with proposed
  order instructions" than a fast alert.
- Independent process/database from the existing autotrader stack and from
  the swing/day-trading scanners.

## 2. Scan Cadence

- **Primary scan**: monthly (or quarterly, configurable) — fundamentals and
  thematic positioning don't change fast enough to justify more frequent
  runs.
- **Trigger-based re-scan**: also run outside the schedule when a held or
  watched name reports earnings or has a material analyst rating/guidance
  change — this is a re-rank of existing candidates, not a full universe
  re-scan.
- No intraday component at all.

## 3. Universe (configurable per run)

1. **Custom watchlist** — a long-term candidate list the user curates and
   revisits periodically, synced via `create_watchlist`/`edit_watchlist`/
   `get_watchlist(s)`. This is the natural default for a focused,
   already-researched set of names.
2. **Sector/theme subset** — for thematic long-term investing (e.g. "AI
   infrastructure," "GLP-1," "reshoring") via `get_company_themes`,
   `get_theme_details`, and `search_investment_topics` to pull the
   constituent set and surrounding context for a theme.

Universe size isn't rate-limit-constrained the way it is in Design 2 —
monthly batch fundamentals pulls over a few hundred names is manageable.

## 4. Data & IBKR Integration

| Need | Source |
|---|---|
| Fundamentals (growth, margins, valuation, balance sheet, dividends) | Same gap as Design 1: no dedicated fundamentals-data tool is visible in the current IBKR MCP surface, and no financials/fundamentals endpoint is currently called anywhere in `backend-v2` either. **Leading candidate to check first**: `backend-v2` already integrates a Polygon-schema-compatible provider (`massive.com`, via `backend-v2/data/client.py`, auth/rate-limit/backoff/pagination all proven in production) for bars, ticker reference data, snapshots, indicators, and news — but only calls its ticker-reference endpoint (`/v3/reference/tickers/{ticker}`: market cap, shares outstanding, SIC code, employees), not a financials/financial-statements endpoint. Since real Polygon.io exposes such an endpoint (income statement/balance sheet/cash flow), massive.com plausibly does too — unconfirmed, needs a docs/plan check — but if it does, it's cheaper to add one more call to an already-integrated, already-hardened client than to onboard a new vendor or fall back to native IBKR `reqFundamentalData`. |
| Price history (for entry-timing gate, not discovery) | `get_price_history` |
| Current price / VWAP context | `get_price_snapshot` |
| Thematic/sector context | `get_company_themes`, `get_theme_details`, `get_company_connections`, `search_investment_topics` |
| News/catalyst (earnings, guidance, ratings) | `search_investment_topics`, `whats_new` — quarterly cadence makes IBKR's native coverage more likely to be sufficient here than in the day-trading design, since latency isn't critical |
| Current portfolio & allocation | `get_account_positions`, `get_pa_allocation`, `get_pa_performance_all_periods` |
| Buying power / cash | `get_account_balances` |
| Watchlist sync | `create_watchlist`, `edit_watchlist`, `get_watchlist(s)` |
| Proposed trades | `create_order_instruction` |
| Longer-horizon price alerts (e.g. "flag if it drops to my target entry") | `create_alert` |

## 5. Screening & Scoring Pipeline

Same four required signal families, weighted toward the opposite end of the
spectrum from Design 2:

1. **Fundamentals** — the dominant signal: revenue/earnings growth trend,
   margin trend and stability, balance sheet health (debt/cash), valuation
   relative to growth (e.g. PEG-style framing), and — if relevant to the
   user's goals — dividend growth/coverage. This is the primary ranking
   driver.
2. **News/catalyst** — quarterly earnings quality (beat/miss and *why*),
   guidance direction, analyst rating trend, and thematic tailwind from
   sector/theme data. Used to re-rank and to flag "why now" for a candidate
   that's fundamentally attractive but needs a timing reason.
3. **Technical** — a light-touch entry-timing gate only: is the stock
   reasonably placed relative to its 50/200-day trend (e.g. flag but don't
   disqualify if it's extended far above its long-term average, suggesting
   waiting for a pullback rather than chasing).
4. **VWAP** — narrowest role of all three designs: used only to flag
   whether a proposed entry is happening at a locally elevated price on the
   day the order instruction is drafted, as a minor execution-quality note,
   not a scoring input.

## 6. Approval-Gated Output — portfolio-aware

1. Monthly/quarterly scan produces a ranked candidate list plus, separately,
   a rebalancing view: current holdings (`get_account_positions`,
   `get_pa_allocation`) compared against target allocation/concentration
   rules, surfacing any names worth trimming.
2. A research report is generated (ranked new candidates + proposed
   trims/adds), not a fast alert — this is meant to be read and considered,
   not reacted to within minutes.
3. User reviews holistically against the whole portfolio, not just each
   candidate in isolation.
4. For each approved add/trim:
   - `create_order_instruction` recorded, sized using
     `get_account_balances`/`get_pa_allocation` to hit a target position
     size or trim amount rather than an arbitrary quantity.
   - Optionally, a `create_alert` set at a preferred entry price if the
     user wants to wait for a pullback rather than buy immediately.
5. Nothing executes automatically — same rule as the other two designs.

## 7. Architecture Components

- **Scheduler**: monthly/quarterly cron-style trigger, plus an
  earnings-calendar-driven re-scan trigger for held/watched names.
- **Universe resolver**: same pattern as Design 1 — resolves watchlist vs
  theme config to a symbol list.
- **Fundamentals ingestion layer**: the most important data pipeline in this
  design; needs the resolved data-source decision from §4 before it can be
  built.
- **Scoring engine**: fundamentals-dominant composite, with technical/VWAP
  as gates/annotations rather than score drivers.
- **Portfolio-awareness layer**: pulls current positions/allocation and
  folds concentration/diversification rules into the ranking (e.g. down-
  weight or flag a candidate that would push a sector over a concentration
  cap).
- **Report generator**: produces the periodic research report + rebalance
  proposal.
- **Approval queue**: persisted proposal state, but with a much longer
  natural shelf life than Design 1 or 2 — a long-term candidate proposed
  this month is still probably relevant next month, so no aggressive
  expiry.
- **Action layer**: same principle as the other two designs — sole owner of
  IBKR write calls, gated on an approval record.
- **Own SQLite DB** (e.g. `scanner_position.db`), separate from
  `autotrader.db`, `backend_v2.db`, and the other two scanners' databases.

## 8. Risk Controls

- Concentration caps enforced against `get_pa_allocation` (sector, single-
  name, and theme-level) before a candidate is proposed, not just noted
  after the fact.
- Valuation guardrail: candidates trading at extreme valuation multiples
  relative to their own history/peers are flagged, even if growth momentum
  is strong — this design explicitly should not chase pure momentum.
- Rebalance proposals sized against actual account balances/allocation, not
  fixed dollar amounts.

## 9. Open Questions

- Resolving fundamentals-data access is the critical blocker for this
  design specifically — more so than for Design 1, since fundamentals are
  the primary signal here rather than a gate. First step: check whether
  `massive.com` (already integrated in `backend-v2`) exposes a
  financials/fundamentals endpoint beyond the ticker-reference data
  currently used — see §4.
- Decide the concentration/diversification rule set (sector caps, single-
  name max weight, etc.) — currently undefined, needed before the
  portfolio-awareness layer can enforce anything.
- Decide report format/delivery cadence (does a trigger-based earnings
  re-scan interrupt the user immediately, or batch into the next scheduled
  report?).
