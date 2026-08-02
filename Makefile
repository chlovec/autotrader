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

.PHONY: help run-alpaca-sim run-alpaca-live run-ibkr-sim run-ibkr-live research backend dashboard stop restart docker-build docker-up docker-down docker-logs

help:
	@echo "Autoloader run targets (all run $(RUN_SCRIPT); override with RUN_SCRIPT=run.py):"
	@echo "  run-alpaca-sim   Alpaca paper trading (starts backend & dashboard automatically)"
	@echo "  run-alpaca-live  Alpaca with real money (asks for confirmation first)"
	@echo "  run-ibkr-sim     IBKR paper trading, TWS port $(IBKR_SIM_PORT)"
	@echo "  run-ibkr-live    IBKR with real money, TWS port $(IBKR_LIVE_PORT) (asks for confirmation first)"
	@echo "  research         Screen run_research.py's symbol universe and update the watchlist (no trades placed)"
	@echo "  backend          Start the FastAPI backend API on $(BACKEND_HOST):$(BACKEND_PORT)"
	@echo "  dashboard        Start the React dashboard (UI) dev server - needs 'backend' running in another terminal"
	@echo "  restart          Background backend+dashboard+trading loop+research (bin/restart.sh); BROKER= or interactive"
	@echo "  stop             Kill whatever 'restart' (or a previous manual run) started (bin/stop.sh)"
	@echo "  docker-build     Build the backend/engine and dashboard images"
	@echo "  docker-up        Start backend+engine+dashboard in containers (see docker-compose.yml)"
	@echo "  docker-down      Stop and remove the containers docker-up started"
	@echo "  docker-logs      Tail logs from all running containers"

run-alpaca-sim:
	$(PYTHON) $(RUN_SCRIPT) --broker alpaca --alpaca-paper true & \

run-alpaca-live:
	@echo "This will place REAL trades with REAL money on your live Alpaca account."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted."; exit 1)
	$(PYTHON) $(RUN_SCRIPT) --broker alpaca --alpaca-paper false

run-ibkr-sim:
	$(PYTHON) $(RUN_SCRIPT) --broker ibkr --ibkr-port $(IBKR_SIM_PORT)

run-ibkr-live:
	@echo "This will place REAL trades with REAL money on your live IBKR account."
	@echo "Make sure TWS/Gateway is logged into your LIVE account on port $(IBKR_LIVE_PORT), not paper."
	@read -p "Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted."; exit 1)
	$(PYTHON) $(RUN_SCRIPT) --broker ibkr --ibkr-port $(IBKR_LIVE_PORT)

research:
	$(PYTHON) run_research.py

backend:
	$(UVICORN) backend.app.main:app --host $(BACKEND_HOST) --port $(BACKEND_PORT)

dashboard:
	cd frontend && npm run dev

# BROKER deliberately has no default here (unlike RUN_SCRIPT etc. above) - leaving it
# unset lets bin/restart.sh fall through to its own interactive prompt. Paper vs live
# isn't a restart.sh concept at all - it comes entirely from .env (ALPACA_PAPER, etc).
restart:
	@bin/restart.sh $(BROKER)

stop:
	@bin/stop.sh

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f