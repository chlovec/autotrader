# Autotrader

An autotrader for [Alpaca](https://alpaca.markets). It connects to your Alpaca
account, decides what to trade based on a pluggable strategy, places orders
automatically, and exposes a dashboard to monitor what it's doing. The goal is to
trade a real account with real money; it currently runs against Alpaca's paper
(simulated) account while the strategy and infrastructure are being validated, with
live trading a one-line config flip away once you're ready — see
[Going live](#going-live-real-money).

**Current strategy**: an equal-weight SPY / TLT / GLD portfolio, rebalanced monthly.
This was chosen after backtesting several single-asset signal strategies
(moving-average crossover, RSI mean-reversion, an ADX regime-switching composite) —
none of them beat simply holding SPY on a risk-adjusted basis, while the diversified
portfolio did. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full strategy comparison
and how everything fits together.

## Prerequisites

- Python 3.11+ (developed on 3.14)
- Node 20+ (developed on 22)
- An [Alpaca](https://alpaca.markets) account with paper-trading API keys (free)

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

Edit `.env` and fill in `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from your
[Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview). Every
field is documented in `.env.example`; see [Configuration](#configuration) below for
what each one does.

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

To run one of the alternative single-asset strategies instead (see
[ARCHITECTURE.md](ARCHITECTURE.md) for what each does and why they aren't the
default), edit `run.py` to pick a strategy and run that instead:

```bash
.venv/bin/python run.py
```

Don't run both at once against the same account unless you've deliberately sized
`MAX_POSITION_SIZE_USD` for that — they aren't aware of each other's positions.

### 2. The backend API

```bash
nohup .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 > backend.log 2>&1 &
```

Serves `/positions`, `/equity`, `/trades`, `/signals`, `/events`, `/kill-switch`, and
a `/ws` WebSocket, all reading from the same SQLite database the engine writes to
(plus live calls to Alpaca for `/positions`). Health check: `curl localhost:8000/health`.

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
| `BROKER` | Which broker to trade through. `alpaca` is the only one implemented — see [ARCHITECTURE.md](ARCHITECTURE.md#brokers--the-only-place-that-talks-to-alpaca) for what adding another looks like |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Your Alpaca API credentials — only read when `BROKER=alpaca` |
| `ALPACA_BASE_URL` | Alpaca endpoint (paper by default) |
| `ALPACA_PAPER` | `true` for paper trading, `false` for live — see [Going live](#going-live-real-money) |
| `DATABASE_URL` | Where trades/signals/equity history is stored (SQLite file by default) |
| `MAX_POSITION_SIZE_USD` | Per-symbol position cap for the signal-based strategies (`run.py`) — not used by the portfolio rebalancer, which sizes by target weight instead |
| `MAX_DAILY_LOSS_USD` | If today's equity drop exceeds this, both runners halt all trading for the day |
| `SMTP_*` / `ALERT_EMAIL_*` | Optional email alerts on errors, kill-switch trips, and daily-loss halts. Leave blank to skip — macOS notifications fire regardless, with no setup needed |

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

35 tests covering strategy logic, risk checks, portfolio rebalancing math, and the
notification system — all using synthetic data or in-memory databases, no network
or Alpaca credentials required.

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
historical Alpaca data (requires API keys — market data works fine with paper
credentials), so results are actually predictive of what the live runner would have
done. `optimize_ma_crossover.py` and `optimize_regime_switching.py` sweep parameters
for those two strategies. See [ARCHITECTURE.md](ARCHITECTURE.md) for the results that
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
8. Keep `.env` off the server's filesystem in version control and out of any logs
   — it holds your Alpaca keys and (if configured) email credentials.

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
3. **Update `.env`**:

   ```bash
   ALPACA_API_KEY=<your live key>
   ALPACA_SECRET_KEY=<your live secret>
   ALPACA_BASE_URL=https://api.alpaca.markets
   ALPACA_PAPER=false
   ```

4. **Start small.** Fund the live account with less than you're ultimately willing
   to allocate, and/or lower `MAX_POSITION_SIZE_USD`, so a bug costs a bounded
   amount to discover rather than the full account.
5. **Everything else keeps working the same way** — the kill switch, the daily loss
   halt, the alerts, the dashboard. They're the same code paths regardless of
   `ALPACA_PAPER`; nothing extra to configure for them to apply to the live account.

Nothing in this codebase enforces steps 1–4 for you — the flag change alone is
enough to start placing real orders, so the caution has to come from you, not from
the software.
