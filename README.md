# Autotrader

An autotrader that connects to your brokerage account, decides what to trade based
on a pluggable strategy, places orders automatically, and exposes a dashboard to
monitor what it's doing. Three brokers are supported — [Alpaca](https://alpaca.markets),
[Interactive Brokers](https://www.interactivebrokers.com), and
[Questrade](https://www.questrade.com) — selected with one `BROKER` env var; see
[Brokers](#brokers) for setup. The goal is to trade a real account with real money;
it's currently validated against Alpaca's paper (simulated) account while the
strategy and infrastructure prove themselves out, with live trading a one-line
config flip away once you're ready — see [Going live](#going-live-real-money).

**Current strategy**: an equal-weight SPY / TLT / GLD portfolio, rebalanced monthly.
This was chosen after backtesting several single-asset signal strategies
(moving-average crossover, RSI mean-reversion, an ADX regime-switching composite) —
none of them beat simply holding SPY on a risk-adjusted basis, while the diversified
portfolio did. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full strategy comparison
and how everything fits together.

## Prerequisites

- Python 3.11+ (developed on 3.14)
- Node 20+ (developed on 22)
- An account with one of the supported brokers — see [Brokers](#brokers) for what
  each one needs

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

Edit `.env`: set `BROKER` to the one you're using, then fill in that broker's
section below it (see [Brokers](#brokers) for what each needs). Every field is also
documented inline in `.env.example`; see [Configuration](#configuration) for a
summary table.

## Brokers

Three are supported, picked via the `BROKER` env var. Only Alpaca has actually been
run against a live account by this project — see the caveat under each.

### Alpaca (`BROKER=alpaca`)

The most complete integration, and the only one verified end-to-end (real paper
trades placed, confirmed via the dashboard). Sign up at
[alpaca.markets](https://alpaca.markets), grab paper-trading API keys from the
[paper dashboard](https://app.alpaca.markets/paper/dashboard/overview), and set
`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in `.env`. No local software needed —
it's a pure REST/WebSocket API.

### Interactive Brokers (`BROKER=ibkr`)

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
3. Set `IBKR_PORT` in `.env` to match: `7497` for TWS paper (the default),
   `7496` for TWS live, `4002` for Gateway paper, `4001` for Gateway live.
4. Leave TWS/Gateway running whenever the engine runs — there's no key/secret to
   configure because authentication *is* being logged into that software.

### Questrade (`BROKER=questrade`)

**Untested against a live account** — built against Questrade's public API
documentation, covered by tests against mocked HTTP responses matching that
documented shape, but no Questrade account was available to confirm field names
against a real response. Verify it yourself before trusting it with money.

1. Get a refresh token from the
   [App Hub](https://login.questrade.com/APIAccess/UserApps.aspx) (use a
   [practice account](https://www.questrade.com/api/documentation/getting-started)
   for simulated trading — same API, separate App Hub/token).
2. Set `QUESTRADE_REFRESH_TOKEN` in `.env` to that token.
3. That's it for setup, but know this going in: Questrade refresh tokens are
   **single-use** — every API session exchanges it for a new one, invalidating the
   old. This app handles that automatically (the current token gets cached in
   `questrade_token.json`, gitignored, which takes over from the `.env` value after
   the first run), but if you ever manually re-generate a token in the App Hub
   while a cached one already exists, delete `questrade_token.json` first so the
   new seed token actually gets used.

## Running it

Three independent processes make up the running app. Each is a plain long-running
process — there's no process manager wiring them together, so open a separate
terminal (or use `nohup ... &`) for each.

### 1. The trading engine

```bash
.venv/bin/python run_portfolio.py
```

Runs the live diversified-portfolio strategy: rebalances SPY/TLT/GLD to equal weight
once a month (first trading day of the month, 9:35am ET), and once immediately on
startup. Logs to stdout — redirect to a file if running unattended:

```bash
nohup .venv/bin/python run_portfolio.py > portfolio_runner.log 2>&1 &
```

A `Makefile` wraps this with the broker/mode combination baked in, so you don't
have to remember which env vars to set:

```bash
make run-alpaca-sim    # Alpaca, paper trading
make run-alpaca-live   # Alpaca, real money - asks you to type "yes" first
make run-ibkr-sim      # IBKR, paper trading (TWS port 7497)
make run-ibkr-live     # IBKR, real money (TWS port 7496) - asks you to type "yes" first
make help              # list targets and current port/script settings
```

These set `BROKER` and the paper/live flag as env vars for that one invocation —
they don't touch `.env`, and `ALPACA_PAPER=false` automatically picks up
`ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY` instead of the paper pair (see
[Configuration](#configuration)). Using IB Gateway instead of TWS, or non-default
ports? `make run-ibkr-sim IBKR_SIM_PORT=4002`. Want to run one of the alternative
single-asset strategies instead of the deployed portfolio rebalancer? `make
run-alpaca-sim RUN_SCRIPT=run.py` (edit `run.py` first to pick which strategy —
see [ARCHITECTURE.md](ARCHITECTURE.md) for what each does and why they aren't the
default).

Don't run two of these at once against the same account unless you've deliberately
sized `MAX_POSITION_SIZE_USD` for that — they aren't aware of each other's positions.

### 2. The backend API

```bash
nohup .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 > backend.log 2>&1 &
```

Serves `/positions`, `/equity`, `/trades`, `/signals`, `/events`, `/kill-switch`, and
a `/ws` WebSocket, all reading from the same SQLite database the engine writes to
(plus one live call to whichever broker is configured for `/positions`). Health
check: `curl localhost:8000/health`.

### 3. The dashboard

```bash
cd frontend && nohup npm run dev > ../frontend.log 2>&1 &
```

Open `http://localhost:5173`. It polls the backend every 15 seconds and listens on
the WebSocket for live equity ticks. The kill switch button is real — it flips the
same database row the engine checks before every trading cycle.

### Stopping everything

Each process was started with `&`; find and kill them by PID (printed on start) or:

```bash
pkill -f run_portfolio.py
pkill -f "uvicorn backend.app.main"
pkill -f "vite"
```

## Configuration

All configuration is environment variables, loaded from `.env` (see `.env.example`
for the full list with defaults):

| Variable | Purpose |
| --- | --- |
| `BROKER` | `alpaca`, `ibkr`, or `questrade` — see [Brokers](#brokers). Implementation details in [ARCHITECTURE.md](ARCHITECTURE.md) |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Your Alpaca **paper** API credentials — only read when `BROKER=alpaca` and `ALPACA_PAPER=true` |
| `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY` | Your Alpaca **live** API credentials (a separate account) — only read when `BROKER=alpaca` and `ALPACA_PAPER=false` |
| `ALPACA_BASE_URL` | Alpaca endpoint (paper by default) |
| `ALPACA_PAPER` | `true` for paper trading, `false` for live — see [Going live](#going-live-real-money). `make run-alpaca-sim`/`run-alpaca-live` set this for you |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` | Connection details for a locally running TWS/Gateway — only read when `BROKER=ibkr` |
| `QUESTRADE_REFRESH_TOKEN` | Seed OAuth token — only read when `BROKER=questrade`, and only until the first run (see [Brokers](#brokers)) |
| `DATABASE_URL` | Where trades/signals/equity history is stored (SQLite file by default) |
| `MAX_POSITION_SIZE_USD` | Per-symbol position cap for the signal-based strategies (`run.py`) — not used by the portfolio rebalancer, which sizes by target weight instead |
| `MAX_DAILY_LOSS_USD` | If today's equity drop exceeds this, both runners halt all trading for the day |
| `SMTP_*` / `ALERT_EMAIL_*` | Optional email alerts on errors, kill-switch trips, and daily-loss halts. Leave blank to skip — macOS notifications fire regardless, with no setup needed |

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

65 tests covering strategy logic, risk checks, portfolio rebalancing math, the
notification system, config's paper/live key selection, and all three broker
integrations — all using synthetic data, in-memory databases, or mocked
network/socket calls, no live credentials or network
access required.

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

- **Kill switch**: a manual stop, toggleable from the dashboard or `POST
  /kill-switch`. Checked before every trading cycle.
- **Daily loss limit**: if equity drops more than `MAX_DAILY_LOSS_USD` since the
  first snapshot of the day, both runners halt all trading until the next day.
- **Alerts**: order failures, kill-switch engagements, and daily-loss halts trigger
  a macOS notification and (if configured) an email — not just a log line.

None of this replaces watching the account yourself, especially in the first weeks.

## Deploying

Everything above runs as plain local processes tied to whatever machine started
them — if that machine sleeps, restarts, or the terminal closes, trading stops
until you start it again. There's no Docker image or CI pipeline; deploying just
means running the same commands somewhere that stays on.

Unattended deployment (`launchd`, `systemd`) should invoke `.venv/bin/python
run_portfolio.py` directly with explicit `BROKER`/`ALPACA_PAPER`/`IBKR_PORT` env
vars, **not** `make run-alpaca-live` / `make run-ibkr-live` — those two prompt for
interactive confirmation on purpose, which just hangs forever with no terminal
attached to answer it. The `make` targets are for you, running something by hand
and meaning to; a service definition should already encode that intent explicitly
in its own config, the way the example below does.

### Option A: keep it on your Mac, survive reboots

Use `launchd` to keep the three processes running and restart them if they crash
or the machine reboots. Example plist for the trading engine
(`~/Library/LaunchAgents/com.autotrader.portfolio.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.autotrader.portfolio</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/autotrader/.venv/bin/python</string>
    <string>/path/to/autotrader/run_portfolio.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/autotrader</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/autotrader/portfolio_runner.log</string>
  <key>StandardErrorPath</key><string>/path/to/autotrader/portfolio_runner.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.autotrader.portfolio.plist`.
Repeat for the backend (point `ProgramArguments` at `.venv/bin/uvicorn` with the
same args used above). The dashboard is dev-only tooling for now (see below) — it
doesn't need to run unattended the way the engine and backend do.

### Option B: an always-on server/VM

More robust than relying on a laptop staying awake. On a fresh Linux box:

Note if you're on `BROKER=ibkr`: TWS/Gateway would need to run on that same server
too (or something needs to bridge to wherever it runs) — it's real, GUI-adjacent
software you log into, not a REST API. This makes IBKR the awkward fit for
headless server deployment; Alpaca and Questrade, both pure remote APIs, don't
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
   switch, in particular, is an unauthenticated `POST` today. That's acceptable
   only because it's bound to `127.0.0.1` and never exposed publicly.
7. macOS notifications obviously don't work on a Linux server — configure the
   `SMTP_*` / `ALERT_EMAIL_*` variables so you still get alerted.
8. Keep `.env` (and, if using Questrade, `questrade_token.json`) out of version
   control and out of any logs — they hold your broker credentials and (if
   configured) email credentials.

### Going live (real money)

This is the actual destination — paper trading is validation, not the end state.
Flipping to live trading is a small config change; the judgment call of *when* is
the part that matters:

1. **Open (or upgrade to) a live Alpaca account.** This is a separate account from
   the paper one, requires identity verification and funding, and is a distinct set
   of API keys from `app.alpaca.markets` (not the paper dashboard).
2. **Watch the paper-trading results for a meaningful stretch first** — weeks, not
   days, and ideally through at least one real rebalance cycle so you've seen the
   full loop (signal → order → fill → dashboard update) work unattended, not just
   in a one-off test.
3. **Add the live keys to `.env`**, alongside the paper ones already there — don't
   replace them:

   ```bash
   ALPACA_LIVE_API_KEY=<your live key>
   ALPACA_LIVE_SECRET_KEY=<your live secret>
   ```

4. **Start small.** Fund the live account with less than you're ultimately willing
   to allocate, and/or lower `MAX_POSITION_SIZE_USD`, so a bug costs a bounded
   amount to discover rather than the full account.
5. **Run `make run-alpaca-live`** instead of `run-alpaca-sim` when you actually
   mean to go live — it sets `ALPACA_PAPER=false` for that one run (which picks up
   the live keys from step 3) and asks you to type `yes` first. `.env` itself never
   needs to change, so `make run-alpaca-sim` stays available for paper testing at
   any time without undoing anything.
6. **Everything else keeps working the same way** — the kill switch, the daily loss
   halt, the alerts, the dashboard. They're the same code paths regardless of
   `ALPACA_PAPER`; nothing extra to configure for them to apply to the live account.

Nothing in this codebase enforces steps 1–4 for you — typing `yes` at the `make
run-alpaca-live` prompt is enough to start placing real orders, so the caution has
to come from you, not from the software.
