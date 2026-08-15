# Design 2 — Day Trading Scanner (IBKR)

Holding period: intraday only (minutes to hours, flat by close). This is the
most demanding of the three designs — it needs near-real-time data, which
the currently available IBKR MCP tools are not built for.

## 1. Scope & Goals

- Detect intraday setups (VWAP reclaim/rejection, opening-range breakout,
  relative-volume spikes, gap-and-go) as they happen, not after the fact.
- Push a time-boxed proposal to the user the moment a setup triggers; if not
  approved within a short window, the proposal expires (a day-trade setup
  from 10 minutes ago is often no longer valid).
- Same hard rule as the other designs: no autonomous order placement. The
  scanner proposes, the human approves, only then does an order instruction
  get created.
- Independent process/database from the existing autotrader stack and from
  the swing/position scanners — this one has fundamentally different
  infrastructure needs (streaming vs batch) and shouldn't share a runtime.

## 2. Scan Cadence — and a key infrastructure gap

Day trading needs continuous, low-latency data during market hours:
1-minute (or finer) bars, live quotes, and real-time relative volume — not
periodic polling.

The IBKR MCP tools currently available (`get_price_snapshot`,
`get_price_history`) are snapshot/pull-based. Calling them in a tight loop
for a watchlist of symbols will be both rate-limited and laggy — not
suitable as the primary data path for this design.

**Implication**: this design needs a direct IBKR API connection —
TWS/IB Gateway via the native socket API (e.g. `ibapi` or `ib_insync`),
using `reqMktData`/`reqRealTimeBars`/`reqTickByTickData` for streaming —
running as its own persistent process, separate from the MCP layer. The
MCP tools are still useful for secondary, on-demand actions: pulling
option data for a flagged symbol (`get_option_data`, `get_option_parameters`),
checking a snapshot for a manual sanity check, and — critically — creating
the watchlist/alert/order-instruction once a human approves a proposal.

This is the one design of the three where "IBKR integration" really means
two separate integrations: a streaming data connection for detection, and
the MCP action layer for output.

## 3. Universe (configurable per run) — must stay small

Because live market data lines are a limited resource per IBKR account/
subscription tier, this design cannot scan the broad market continuously.
Both supported universe modes are intentionally bounded:

1. **Custom watchlist** — a short, hand-picked list (e.g. 10–40 liquid
   names) synced via `create_watchlist`/`get_watchlist(s)`. This is the
   default recommendation for day trading: a small, known, liquid universe.
2. **Sector/theme subset** — via `get_company_themes`/`get_theme_details`,
   but capped to a hard maximum symbol count before the scan starts, to
   stay within streaming data-line limits.

A pre-market filter (float size, average volume, price range, market cap)
should further trim whichever universe is chosen down to names that are
actually tradable intraday before subscribing to live data for them.

## 4. Data & IBKR Integration

| Need | Source |
|---|---|
| Streaming quotes / 1-min bars | Native TWS/IB Gateway socket API (`reqMktData`, `reqRealTimeBars`) — outside the MCP layer |
| Intraday VWAP (computed live from the streamed bars) | Computed locally from streamed volume/price, not fetched — needs to be accurate tick-by-tick, not a polled approximation |
| Relative volume vs recent average | Streamed cumulative volume vs a baseline pulled once via `get_price_history` at session start |
| Pre-market gap / float / liquidity filter | `get_price_snapshot` + `get_price_history` (cheap, once per symbol at the start of the session — not in the hot loop) |
| News/catalyst trigger | This needs to be fast — earnings surprises, halts, press releases. IBKR's coverage via MCP tools (`search_investment_topics`, `whats_new`) is likely too slow/thin for this use case; a dedicated low-latency news feed is probably required as a separate integration. Flagged as open question. |
| Option context on a flagged symbol (optional, on demand) | `get_option_data`, `get_option_parameters` |
| Watchlist sync | `create_watchlist`, `edit_watchlist`, `get_watchlist(s)` |
| Approved-proposal alert | `create_alert` |
| Proposed trade | `create_order_instruction` |
| Account/risk context | `get_account_balances`, `get_account_positions` |

## 5. Screening & Scoring Pipeline

Same four required signal families, but weighted very differently from the
swing/position designs:

1. **VWAP** — the primary signal, not a timing overlay: reclaim from below,
   rejection from above, or a clean hold above VWAP as trend confirmation.
   Computed live, tick by tick.
2. **Technical** — opening-range breakout, relative-volume spike (e.g. >3x
   the trailing-20-day average pace for the time of day), short-term
   momentum (1–5 min).
3. **News/catalyst** — the *trigger* for most day-trade setups (earnings
   surprise, guidance, halt-and-resume, breaking news). Weighted heavily —
   a pure technical pattern with no catalyst is a much weaker candidate
   intraday than one with news behind it.
4. **Fundamentals** — used only as a coarse pre-market filter (market cap
   range, float size, avg dollar volume) to keep the universe tradable and
   liquid — not as an ongoing score component. Fundamentals don't move fast
   enough to matter within a trading session.

## 6. Approval-Gated Output — time-boxed

Because setups are perishable, the approval flow differs from the other two
designs:

1. A live detector process watches the (small) universe throughout the
   session.
2. On trigger, a proposal is generated immediately: symbol, setup type,
   suggested entry/stop/target, and the catalyst/context that fired it.
3. Proposal is pushed to the user with a short validity window (e.g. 60–90
   seconds) — after which it auto-expires and is logged as "missed," not
   silently re-offered stale.
4. If approved within the window: `create_order_instruction` is created
   (and/or `create_alert` if the entry hasn't triggered yet), sized against
   live `get_account_balances`/`get_account_positions`.
5. If not approved in time: discarded. No queued backlog of stale intraday
   setups — unlike swing/position, "review later" isn't meaningful here.

## 7. Architecture Components

- **Streaming detector process**: long-running, holds the IB Gateway socket
  connection, computes VWAP/rel-volume/ORB in-memory per symbol for the
  session.
- **Pre-market prep job**: once daily before the open — resolves universe,
  applies the liquidity/float filter, pulls the volume baseline via
  `get_price_history`.
- **Catalyst feed listener**: separate input stream (news/earnings), feeding
  triggers into the detector.
- **Proposal/approval channel**: needs to be fast (push notification, not a
  digest email) given the expiry window — the notification mechanism choice
  is a hard dependency here, more so than in the other two designs.
- **Action layer**: same principle as the other designs — the only
  component allowed to call IBKR write endpoints, gated strictly on an
  approval record, but here the gate has a clock attached.
- **Session log DB**: own SQLite file (e.g. `scanner_daytrading.db`) logging
  every trigger, every proposal, and its outcome (approved/expired/
  rejected) for later review — separate from `autotrader.db` and
  `backend_v2.db`, and separate from the swing/position scanners' DBs too,
  since this one writes at a much higher frequency.

## 8. Risk Controls

- Hard daily circuit breaker: after N consecutive losing approved trades (or
  a max daily loss threshold checked against `get_account_balances`), the
  detector stops surfacing new proposals for the rest of the session.
- Pattern Day Trader rule awareness: check `get_account_balances`/account
  equity before proposing a new round-trip if the account is near the PDT
  threshold.
- Max concurrent open proposals capped, to avoid flooding the approval
  channel during a volatile open.

## 9. Open Questions

- Native TWS/IB Gateway streaming connection needs to be stood up outside
  the current MCP-only tool access — confirm this is feasible in whatever
  environment ends up running this process (it needs a persistent
  socket-level connection to Gateway/TWS, not just MCP calls).
- Low-latency news/catalyst source — IBKR MCP tools alone are likely
  insufficient; needs a decision on a supplementary feed.
- Notification mechanism for the ~60–90s approval window — push-capable,
  not a batch digest.
- VWAP computation correctness needs validation against IBKR's own VWAP (if
  exposed) before trusting a locally-computed version for live decisions.
=
=
=

Getting TWS/IB Gateway API access involves both account-level setup (with IBKR) and local software setup. Here's the practical path:

1. Account-level (IBKR Client Portal)
You need an IBKR account (paper trading account is enough to start — recommended before touching a live account for this).
Log into Client Portal → Settings → API → Settings, and enable API access for the account.
Market data subscriptions — this is the part most likely to bite you: real-time streaming quotes for day trading require paid subscriptions per exchange/data feed (e.g., NYSE/ARCA/NASDAQ level 1, possibly OPRA for options). Without them, reqMktData/reqRealTimeBars silently return 15–20 min delayed data, which is useless for the VWAP/relative-volume signals this design depends on. Check Client Portal → Settings → Market Data Subscriptions and add what's needed for your target universe's exchanges.
2. Local software
Download IB Gateway (not full TWS) — it's the headless, lightweight option meant for exactly this kind of persistent API connection, no trading UI overhead.
Install it, log in once manually to confirm credentials work.
In Gateway: Configure → Settings → API → Settings:
Enable "ActiveX and Socket Clients"
Note the socket port — paper trading defaults to 4002, live to 4001 (TWS itself uses 7497/7496)
Add 127.0.0.1 to trusted IPs
Decide on "Read-Only API" — since this design routes actual order creation through the MCP layer (create_order_instruction) rather than the socket connection, you can likely leave the socket connection read-only and only use it for streaming market data.
3. Client library
ib_insync is the de facto standard Python wrapper (note: unmaintained as of the original author stepping back — the community fork ib_async is the actively maintained successor). Either wraps IBKR's raw ibapi.
4. The persistent-connection gotcha
This is the part most relevant to your "Open Questions" §9 — Gateway/TWS sessions require periodic re-authentication (roughly every 24h) and IBKR enforces 2FA via IBKR Mobile on login, which complicates fully unattended restarts. The common workaround is IBC (IB Controller), a third-party automation layer that scripts Gateway login/restart. Worth deciding early whether you're comfortable running that, or want to accept manual daily re-login as an operational constraint for now.

Bottom line for this design: the two real blockers to confirm before writing streaming-detector code are (a) whether you're willing to pay for real-time data subscriptions for your target universe's exchanges, and (b) how you want to handle the 2FA/session-restart problem for a long-running unattended process. Want help figuring out the subscription cost for a specific set of exchanges, or looking at what an IBC-based restart setup would look like?