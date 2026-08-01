SHELL := /bin/bash

PYTHON ?= .venv/bin/python
UVICORN ?= .venv/bin/uvicorn
RUN_SCRIPT ?= run_portfolio.py

# Standard Trader Workstation ports. Using IB Gateway instead of TWS, or a
# non-default port? Override on the command line, e.g.:
#   make run-ibkr-sim IBKR_SIM_PORT=4002
IBKR_SIM_PORT ?= 7497
IBKR_LIVE_PORT ?= 7496

BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8000

.PHONY: help run-alpaca-sim run-alpaca-live run-ibkr-sim run-ibkr-live research backend dashboard

help:
	@echo "Autoloader run targets (all run $(RUN_SCRIPT); override with RUN_SCRIPT=run.py):"
	@echo "  run-alpaca-sim   Alpaca paper trading (starts backend & dashboard automatically)"
	@echo "  run-alpaca-live  Alpaca with real money (asks for confirmation first)"
	@echo "  run-ibkr-sim     IBKR paper trading, TWS port $(IBKR_SIM_PORT)"
	@echo "  run-ibkr-live    IBKR with real money, TWS port $(IBKR_LIVE_PORT) (asks for confirmation first)"
	@echo "  research         Screen run_research.py's symbol universe and update the watchlist (no trades placed)"
	@echo "  backend          Start the FastAPI backend API on $(BACKEND_HOST):$(BACKEND_PORT)"
	@echo "  dashboard        Start the React dashboard (UI) dev server - needs 'backend' running in another terminal"

run-alpaca-sim:
	@echo "Starting backend, dashboard, and Alpaca sim..."
	@trap 'kill 0' EXIT; \
	make backend & \
	make dashboard & \
	BROKER=alpaca ALPACA_PAPER=true $(PYTHON) $(RUN_SCRIPT) & \
	wait

run-alpaca-live:
	@echo "This will place REAL trades with REAL money on your live Alpaca account."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted."; exit 1)
	BROKER=alpaca ALPACA_PAPER=false $(PYTHON) $(RUN_SCRIPT)

run-ibkr-sim:
	BROKER=ibkr IBKR_PORT=$(IBKR_SIM_PORT) $(PYTHON) $(RUN_SCRIPT)

run-ibkr-live:
	@echo "This will place REAL trades with REAL money on your live IBKR account."
	@echo "Make sure TWS/Gateway is logged into your LIVE account on port $(IBKR_LIVE_PORT), not paper."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted."; exit 1)
	BROKER=ibkr IBKR_PORT=$(IBKR_LIVE_PORT) $(PYTHON) $(RUN_SCRIPT)

research:
	$(PYTHON) run_research.py

backend:
	$(UVICORN) backend.app.main:app --host $(BACKEND_HOST) --port $(BACKEND_PORT)

dashboard:
	cd frontend && npm run dev