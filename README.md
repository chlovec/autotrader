# Autotrader

An autotrader that connects to your brokerage account(s), decides what to trade based
on a pluggable strategy, places orders automatically, and exposes a dashboard to
monitor what it's doing. Trades **multiple accounts** at once, each with its own
broker, credentials, strategy, and risk limits — a main dashboard lists every account's
live summary (equity/cash/P&L), and clicking one opens its own detail page. Three
brokers are supported per account — [Alpaca](https://alpaca.markets),
[Interactive Brokers](https://www.interactivebrokers.com), and
[Questrade](https://www.questrade.com) — see [Brokers](#brokers) for setup. The goal is
to trade real accounts with real money; it's currently validated against Alpaca's paper
(simulated) account while the strategy and infrastructure prove themselves out, with
live trading a one-line config flip away once you're ready — see
[Going live](#going-live-real-money).

**Current default strategy**: an equal-weight SPY / TLT / GLD portfolio, rebalanced
monthly. This was chosen after backtesting several single-asset signal strategies
(moving-average crossover, RSI mean-reversion, an ADX regime-switching composite) —
none of them beat simply holding SPY on a risk-adjusted basis, while the diversified
portfolio did. Each account picks its own strategy independently (see
[Configuration](#configuration)); see [ARCHITECTURE.md](ARCHITECTURE.md) for the full
strategy comparison and how everything fits together.

## Prerequisites

- Python 3.11+ (developed on 3.14)
- Node 20+ (developed on 22)
- An account with one of the supported brokers, for each account you want to trade —
  see [Brokers](#brokers) for what each one needs

## Setup

```bash
# 1. Python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Frontend dependencies
cd frontend && npm install && cd ..

# 3. Configuration
cp .env.example .env
```

Edit `.env`: list every account you want to trade in `ACCOUNT_IDS` (comma-separated
ids, e.g. `alpaca_paper,ibkr_main`), then for each id fill in its
`ACCOUNT_<id>_BROKER` and that broker's fields (see [Brokers](#brokers) for what each
needs). Every field is also documented inline in `.env.example`; see
[Configuration](#configuration) for a summary table. The first time the backend or
engine starts, each id in `ACCOUNT_IDS` gets a row in the database (active by default)
— from then on, activation, strategy limits, and the kill switch are edited from the
dashboard, not `.env`.

## Brokers

Three are supported, picked per-account via that account's `ACCOUNT_<id>_BROKER` var.
Only Alpaca has actually been run against a live account by this project — see the
caveat under each.

### Alpaca (`ACCOUNT_<id>_BROKER=alpaca`)

The most complete integration, and the only one verified end-to-end (real paper
trades placed, confirmed via the dashboard). Sign up at
[alpaca.markets](https://alpaca.markets), grab paper-trading API keys from the
[paper dashboard](https://app.alpaca.markets/paper/dashboard/overview), and set that
account's `ACCOUNT_<id>_ALPACA_API_KEY` / `ACCOUNT_<id>_ALPACA_SECRET_KEY` /
`ACCOUNT_<id>_ALPACA_BASE_URL` in `.env`. No local software needed — it's a pure
REST/WebSocket API.

There's no separate paper/live setting — Alpaca's paper and live environments are just
different hosts (`paper-api.alpaca.markets` vs `api.alpaca.markets`), each with their own
key pair, so whichever credentials + base URL you give an account *is* which environment
it trades in (same idea as IBKR's port number or Questrade's token below). Want one
account paper and another live? Give them different ids in `ACCOUNT_IDS`, each with its
own key pair.

### Interactive Brokers (`ACCOUNT_<id>_BROKER=ibkr`)

**Untested against a live account** — built against `ib_async`'s actual installed
API (method and field names were checked directly, not just remembered from docs),
covered by tests against mocked responses, but no TWS/Gateway instance was
available to confirm real behavior. Verify it yourself before trusting it with
money.

Requires Trader Workstation (TWS) or IB Gateway **running locally** — unlike
Alpaca/Questrade, IBKR isn't a pure remote API, it's a socket connection to
software you keep running and logged into:

1. Install [TWS or IB Gateway](https://www.interactivebrokers.com/en/trading/tws.php)
   and log in (use paper-trading login credentials for a simulated account).
2. Enable API access: File → Global Configuration → API → Settings → check "Enable
   ActiveX and Socket Clients", and make sure "Read-Only API" is **unchecked** (or
   this app can read your account but never place orders).
3. Set that account's `ACCOUNT_<id>_IBKR_PORT` in `.env` to match: `7497` for TWS
   paper (the default), `7496` for TWS live, `4002` for Gateway paper, `4001` for
   Gateway live.
4. If you're connecting more than one IBKR account to the same TWS/Gateway
   instance, give each a distinct `ACCOUNT_<id>_IBKR_CLIENT_ID` — TWS rejects two
   simultaneous connections sharing a client id.
5. Leave TWS/Gateway running whenever the engine runs — there's no key/secret to
   configure because authentication *is* being logged into that software.

### Questrade (`ACCOUNT_<id>_BROKER=questrade`)

**Untested against a live account** — built against Questrade's public API
documentation, covered by tests against mocked HTTP responses matching that
documented shape, but no Questrade account was available to confirm field names
against a real response. Verify it yourself before trusting it with money.

1. Get a refresh token from the
   [App Hub](https://login.questrade.com/APIAccess/UserApps.aspx) (use a
   [practice account](https://www.questrade.com/api/documentation/getting-started)
   for simulated trading — same API, separate App Hub/token).
2. Set that account's `ACCOUNT_<id>_QUESTRADE_REFRESH_TOKEN` in `.env` to that
   token.
3. That's it for setup, but know this going in: Questrade refresh tokens are
   **single-use** — every API session exchanges it for a new one, invalidating the
   old. This app handles that automatically (the current token gets cached in
   `questrade_token_<id>.json`, gitignored, which takes over from the `.env` value
   after the first run), but if you ever manually re-generate a token in the App
   Hub while a cached one already exists, delete that account's
   `questrade_token_<id>.json` first so the new seed token actually gets used.

## Running it

Three independent processes make up the running app. Each is a plain long-running
process — there's no process manager wiring them together, so open a separate
terminal (or use `nohup ... &`) for each. Unlike before, there's only **one** trading
process (`run_engine.py`) regardless of how many accounts or brokers you're trading —
each cycle it loops over every active account and trades it with that account's own
broker connection and assigned strategy.

### Quick start: all of it in one command

`bin/restart.sh` (or `make restart`) backgrounds all three processes plus one
research run for you — the backend, the dashboard, the trading loop, and
`run_research.py` — instead of doing steps 0-3 below by hand across separate
terminals:

```bash
make restart
```

Which broker(s)/paper-vs-live each account uses comes entirely from `.env` (each
account's own `ACCOUNT_<id>_*` vars) — there's no per-run broker selection anymore,
since one run now trades every active account, possibly across several different
brokers at once. Output goes to `services.log`; stop everything it started with:

```bash
make stop
```

`bin/stop.sh` matches processes by command line (backend, dashboard, trading loop,
a still-running research run) rather than tracking PIDs, so it works regardless of
how they were started — including a stray process from a previous session. The
rest of this section covers running each piece by hand, useful if you want them in
separate terminals you can watch individually.

### 0. Research (needs at least one run before any signal-strategy account has anything to trade)

The backend (started in step 2 below) automatically runs research every night at
2am ET, and the dashboard's main page has a "Run research now" button and a "Run
nightly" toggle — once the backend is running, that's normally all you need. To run
it by hand instead (before the backend's first nightly run, or on a box that
doesn't run the backend persistently, e.g. via an OS-level cron job):

```bash
.venv/bin/python run_research.py
# or
make research
```

Screens the fixed symbol universe in `engine/research_runner.py`'s
`DEFAULT_UNIVERSE` (edit that constant to change which tickers are considered),
scores each on a technical (price/volume) and a news layer, and writes the results
to the `research_results` table — see [ARCHITECTURE.md](ARCHITECTURE.md) for how
scoring works, how the backend's nightly job/toggle/button fit together, and which
account's broker connection research borrows for market data. Never places an
order, only reads market data/news and writes to the database.

Research is global, shared by every account assigned a signal-based strategy
(`ma_crossover`, `mean_reversion`, `regime_switching`) — those accounts trade
whichever symbols the most recent research run selected (top 10 by combined score);
if `research_results` is empty, such an account logs a warning and skips its cycle
instead of trading nothing. Accounts assigned `rebalancing_portfolio` are
unaffected — they always trade their own fixed `target_weights`, not the research
watchlist.

### 1. The trading engine

```bash
.venv/bin/python run_engine.py
# or
make run-engine
```

Each cycle (weekdays at 9:35am ET, plus once immediately on startup), loops over
every account marked active in the dashboard and trades it with whatever strategy
it's assigned — a signal strategy trades daily, `rebalancing_portfolio` only
actually rebalances once a month but is checked every cycle (idempotent, so this is
safe). One account's failure is logged and never blocks the others. Logs to
stdout — redirect to a file if running unattended:

```bash
nohup .venv/bin/python run_engine.py > engine.log 2>&1 &
```

Don't run two of these at once — accounts aren't aware of a second process also
acting on their behalf.

### 2. The backend API

```bash
nohup .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 > backend.log 2>&1 &
# or, in the foreground:
make backend
```

Serves `/accounts` (every account's summary), `/accounts/{id}` and its
`/positions`/`/equity`/`/trades`/`/signals`/`/kill-switch`/`/limits` sub-resources,
`/events`, `/research`, `/research/schedule`, `/research/status`, `/research/run`,
and a `/ws/accounts/{id}` WebSocket per account — all reading from the same SQLite
database the engine writes to (plus one live broker call per active account for its
positions). Health check: `curl localhost:8000/health`.

On startup this process opens one broker connection per **active** account and keeps
it open for live position/equity/trade streaming — activating or deactivating an
account from the dashboard opens/closes that connection at runtime. This process
also *runs* research: on startup it registers a nightly job (2am ET, skipped if the
dashboard's "Run nightly" toggle is off) and exposes the "Run research now" trigger
— see [ARCHITECTURE.md](ARCHITECTURE.md#backend--the-api-plus-the-nightly-research-scheduler)
for why that scheduling lives here rather than in `run_research.py`. Research
borrows the first active account's broker connection for market data — the backend
needs at least one active account configured for research to run.

### 3. The dashboard

```bash
cd frontend && nohup npm run dev > ../frontend.log 2>&1 &
# or, in the foreground:
make dashboard
```

Open `http://localhost:5173`. The **main page** lists every account (active and
inactive) with its live equity/cash/unrealized P&L, an activate/deactivate button,
the Research card (scored symbols, "Run nightly" toggle, "Run research now"
button), and System Events. Click an account to open its **detail page**
(`/accounts/<id>`): equity chart, positions, trade log, signal history, the kill
switch (per-account — flips that account's own database row, checked before every
one of *its* trading cycles), and editable trading limits (max position size, max
daily loss). Both pages poll the backend every 15 seconds; the detail page also
listens on that account's WebSocket for live equity/position/trade ticks.

### Stopping everything

```bash
make stop
# or directly:
bin/stop.sh
```

Matches by command line rather than tracked PIDs, so it finds and kills the trading
loop, backend, dashboard, and any still-running research run no matter which of the
methods above started them. Equivalent by hand, if you'd rather not use the script:

```bash
pkill -f run_engine.py
pkill -f "uvicorn backend.app.main"
pkill -f "vite"
```

## Configuration

All configuration is environment variables, loaded from `.env` (see `.env.example`
for the full list with defaults). Two kinds: a handful of genuinely global settings,
and a repeated `ACCOUNT_<id>_*` block per account.

| Variable | Purpose |
| --- | --- |
| `ACCOUNT_IDS` | Comma-separated list of account ids this deployment trades — every id needs a matching `ACCOUNT_<id>_*` block below |
| `ACCOUNT_<id>_BROKER` | `alpaca`, `ibkr`, or `questrade` for that account — see [Brokers](#brokers). Implementation details in [ARCHITECTURE.md](ARCHITECTURE.md) |
| `ACCOUNT_<id>_DISPLAY_NAME` | Shown on the dashboard for that account (defaults to the id itself) |
| `ACCOUNT_<id>_ALPACA_API_KEY` / `_ALPACA_SECRET_KEY` / `_ALPACA_BASE_URL` | That account's Alpaca credentials and endpoint — no separate paper/live flag; whichever key pair + base URL you put here (paper or live) is the environment that account trades in |
| `ACCOUNT_<id>_IBKR_HOST` / `_IBKR_PORT` / `_IBKR_CLIENT_ID` | Connection details for that account's TWS/Gateway — `_IBKR_CLIENT_ID` must be unique per account sharing a TWS/Gateway instance |
| `ACCOUNT_<id>_QUESTRADE_REFRESH_TOKEN` / `_QUESTRADE_POLL_INTERVAL_SECONDS` | Seed OAuth token (only until that account's first run — see [Brokers](#brokers)) and polling cadence for that account's simulated stream |
| `ACCOUNT_<id>_STRATEGY` / `_STRATEGY_PARAMS` | Only read the first time that id appears — after that, the assigned strategy is dashboard/database state, not `.env`. One of `ma_crossover`, `mean_reversion`, `regime_switching`, `rebalancing_portfolio`; `_STRATEGY_PARAMS` is JSON constructor kwargs (e.g. `{"target_weights": {...}}` for the rebalancer) |
| `ALPACA_NEWS_API_KEY` / `ALPACA_NEWS_SECRET_KEY` | Global — research's news layer (`run_research.py`, and the backend's nightly job/"Run research now" button) always uses Alpaca's News API regardless of any account's broker; set a free Alpaca paper key pair here |
| `DATABASE_URL` | Where account state/trades/signals/equity history is stored (SQLite file by default) |
| `MAX_POSITION_SIZE_USD` / `MAX_DAILY_LOSS_USD` / `MAX_TOTAL_EXPOSURE_USD` | Global — only used to seed a newly-discovered account's limits the first time its id appears; edit an account's actual limits from its dashboard page afterward. `MAX_TOTAL_EXPOSURE_USD` caps total open-position value across every symbol at once (0 = no cap, the default) — separate from `MAX_POSITION_SIZE_USD`, which caps one symbol |
| `SMTP_*` / `ALERT_EMAIL_*` | Global. Optional email alerts on errors, kill-switch trips, and daily-loss halts. Leave blank to skip — macOS notifications fire regardless, with no setup needed |

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

132 tests covering strategy logic, risk checks, portfolio rebalancing math, the
notification system, per-account credential loading, the multi-account runner's
dispatch/isolation behavior, and all three broker integrations — all using synthetic
data, in-memory databases, or mocked network/socket calls, no live credentials or
network access required.

## Backtesting

Before trusting any strategy with money (even paper money), backtest it against
historical data:

```bash
.venv/bin/python -m scripts.backtest_ma_crossover
.venv/bin/python -m scripts.backtest_mean_reversion
.venv/bin/python -m scripts.backtest_regime_switching
.venv/bin/python -m scripts.backtest_diversified_portfolio
```

Each mirrors the corresponding live strategy's exact decision logic against
historical data pulled through whichever broker is configured (paper/practice
credentials work fine for this — these backtests were built and run against
Alpaca specifically), so results are actually predictive of what the live runner
would have done. `optimize_ma_crossover.py` and `optimize_regime_switching.py`
sweep parameters for those two strategies. See [ARCHITECTURE.md](ARCHITECTURE.md) for the results that
led to the current default.

## Safety features

- **Kill switch**: a manual stop, per account — toggleable from that account's
  dashboard page or `POST /accounts/{id}/kill-switch`. Checked before every one of
  that account's trading cycles; other accounts are unaffected.
- **Daily loss limit**: per account — if an account's equity drops more than its
  own `max_daily_loss_usd` since the first snapshot of the day, that account halts
  trading until the next day. Editable from its dashboard page.
- **Max total exposure**: per account — caps total open-position value across every
  symbol at once (0 = no cap, the default). Once hit, new buys are blocked (or, for
  `rebalancing_portfolio` accounts, scaled down) until a sale frees up room; sells are
  never blocked by this. Editable from the dashboard alongside the other two limits.
- **Alerts**: order failures, kill-switch engagements, and daily-loss halts trigger
  a macOS notification and (if configured) an email — not just a log line.

None of this replaces watching the accounts yourself, especially in the first weeks.

## Deploying

Everything above runs as plain local processes tied to whatever machine started
them — if that machine sleeps, restarts, or the terminal closes, trading stops
until you start it again. There's no CI pipeline; deploying just means running the
same commands somewhere that stays on, either directly (Options A/B) or in
containers (Option C).

Unattended deployment (`launchd`, `systemd`) should invoke `.venv/bin/python
run_engine.py` directly — every account's broker/mode is resolved from `.env`
(`ACCOUNT_<id>_*`), so there are no CLI flags to pass anymore. **Not** a hand-run
`make` target with an interactive confirmation — this project no longer has one
(there's no single "go live" flag to confirm; each account's paper/live mode is
just its own `.env` value, so treat editing that value as the deliberate action).

### Option A: keep it on your Mac, survive reboots

Use `launchd` to keep the three processes running and restart them if they crash
or the machine reboots. Example plist for the trading engine
(`~/Library/LaunchAgents/com.autotrader.engine.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.autotrader.engine</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/autotrader/.venv/bin/python</string>
    <string>/path/to/autotrader/run_engine.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/autotrader</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/autotrader/engine.log</string>
  <key>StandardErrorPath</key><string>/path/to/autotrader/engine.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.autotrader.engine.plist`.
Repeat for the backend (point `ProgramArguments` at `.venv/bin/uvicorn` with the
same args used above). The dashboard is dev-only tooling for now (see below) — it
doesn't need to run unattended the way the engine and backend do.

### Option B: an always-on server/VM

More robust than relying on a laptop staying awake. On a fresh Linux box:

Note if any account has `BROKER=ibkr`: TWS/Gateway would need to run on that same
server too (or something needs to bridge to wherever it runs) — it's real,
GUI-adjacent software you log into, not a REST API. This makes IBKR the awkward fit
for headless server deployment; Alpaca and Questrade, both pure remote APIs, don't
have this problem.

1. Install Python 3.11+ and Node 20+.
2. Clone the repo, follow [Setup](#setup) above.
3. Switch `DATABASE_URL` to a real database (Postgres) if the backend and engine
   might ever run on different machines — the default SQLite file assumes both
   processes share a filesystem.
4. Use `systemd` units (or a process manager like `supervisord`) instead of
   `launchd` for the engine and backend, so they restart on crash and on boot.
5. Build the frontend for production instead of running the dev server:

   ```bash
   cd frontend && npm run build
   ```

   Serve the resulting `dist/` folder with any static file server (nginx, Caddy,
   `serve`) rather than `npm run dev`.
6. **Put the backend behind authentication before exposing it beyond
   `localhost`.** None of its endpoints require a login right now — the kill
   switch and account activate/deactivate, in particular, are unauthenticated
   `POST`s today. That's acceptable only because it's bound to `127.0.0.1` and
   never exposed publicly.
7. macOS notifications obviously don't work on a Linux server — configure the
   `SMTP_*` / `ALERT_EMAIL_*` variables so you still get alerted.
8. Keep `.env` (and, for any Questrade account, its `questrade_token_<id>.json`)
   out of version control and out of any logs — they hold your broker credentials
   and (if configured) email credentials.

### Option C: Docker Compose

Packages the backend, engine, and dashboard as containers instead of plain
processes — `docker-compose.yml` builds one shared Python image (`Dockerfile`) for
the backend and trading engine, and a separate `node`→`nginx` image
(`frontend/Dockerfile`) that builds the dashboard for production and serves the
static `dist/` output, matching Option B's step 5 above instead of running
`npm run dev`.

```bash
cp .env.example .env   # fill in ACCOUNT_IDS and each account's fields - same file the local setup uses
docker compose up -d --build
```

This starts three services:

- `backend` — the FastAPI API, published on `localhost:8000`
- `engine` — the trading loop (`run_engine.py`, every active account)
- `dashboard` — the built dashboard served by nginx, published on `localhost:5173`

`research` is not part of the default `up` — the backend already runs it nightly
and exposes a "run now" trigger from the dashboard. Run it on demand instead:

```bash
docker compose run --rm research
```

Bring the containers down with:

```bash
docker compose down
```

This stops and removes the `backend`/`engine`/`dashboard` containers but leaves the
bind-mounted `autotrader.db` on disk, so `docker compose up -d --build` picks back
up where you left off. Add `-v` only if you also want to drop any anonymous volumes
Compose created — the SQLite database itself isn't one of those, so it's unaffected
either way.

Notes specific to running this way:

- **Same `.env`, same ports as local dev** — `CORS_ORIGINS=http://localhost:5173`
  and the dashboard's default API base of `http://localhost:8000` (both already in
  `.env.example`) work unchanged, since the browser talks to the containers over
  these published host ports either way.
- **The SQLite database is bind-mounted**, not baked into the image
  (`./autotrader.db`), so it persists across `docker compose down`/`up` the same
  file a non-Docker run would use. **Any Questrade account's token cache** needs
  its own bind mount added to `docker-compose.yml` (`questrade_token_<id>.json`,
  see the comment there) — `touch` it on the host before the first `up`, or Docker
  turns a bind-mount source that doesn't exist yet into a directory instead of a
  file, which breaks the token cache.
- **Any `BROKER=ibkr` account**: TWS/Gateway still runs on your host, not in a
  container — set that account's `IBKR_HOST=host.docker.internal` in `.env` so the
  `engine`/`backend` containers can reach it (already wired up in
  `docker-compose.yml` via `extra_hosts`).
- **No interactive live-trading prompt here** — a backgrounded container has no
  terminal to answer one anyway. The `engine` service just runs whatever every
  account's `.env` fields say, live included — treat editing `.env` to go live as
  the deliberate action.

`make docker-build` / `make docker-up` / `make docker-down` / `make docker-logs`
wrap the equivalent `docker compose` commands.

### Going live (real money)

This is the actual destination — paper trading is validation, not the end state.
There's no paper/live toggle to flip (see [Brokers](#brokers)) — going live means
pointing an account's credentials at the live environment, which this project
recommends doing as a **new account** rather than editing a working paper one in
place, so the two never get mixed up and the paper account stays around (active or
not) as a reference. The judgment call of *when* is the part that matters:

1. **Open (or upgrade to) a live account** with that broker. For Alpaca, this is a
   separate account from the paper one, requires identity verification and
   funding, and is a distinct set of API keys from `app.alpaca.markets` (not the
   paper dashboard).
2. **Watch the paper account's results for a meaningful stretch first** — weeks,
   not days, and ideally through at least one real rebalance cycle so you've seen
   the full loop (signal → order → fill → dashboard update) work unattended, not
   just in a one-off test.
3. **Add a new account id to `ACCOUNT_IDS`** with its own `ACCOUNT_<new-id>_*`
   block using the live credentials and `ACCOUNT_<new-id>_ALPACA_BASE_URL=https://api.alpaca.markets`
   — give it the same strategy (`ACCOUNT_<new-id>_STRATEGY`/`_STRATEGY_PARAMS`) as
   the paper account it's replacing, if you want matching behavior.
4. **Start small.** Fund the live account with less than you're ultimately willing
   to allocate, and/or set a conservative `MAX_POSITION_SIZE_USD`/`MAX_DAILY_LOSS_USD`
   before it's first seeded (or lower them from its dashboard page right after), so
   a bug costs a bounded amount to discover rather than the full account.
5. **Restart the engine/backend** so the new account gets picked up, then
   deactivate the paper account from the dashboard once you're confident (or leave
   both running — activate/deactivate is exactly the tool for stepping one down
   without touching the other).
6. **Everything else keeps working the same way** — the kill switch, the daily loss
   halt, the alerts, the dashboard. They're the same code paths for every account
   regardless of which broker/environment its credentials point at.

Nothing in this codebase enforces steps 1–4 for you — adding a live key pair
to `.env` is enough to start placing real orders on that account, so the caution
has to come from you, not from the software.
