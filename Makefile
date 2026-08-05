SHELL := /bin/bash

PYTHON ?= .venv/bin/python
UVICORN ?= .venv/bin/uvicorn

BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8000

.PHONY: help run-engine research backend dashboard stop restart restart-v2 stop-v2 docker-build docker-up docker-down docker-logs

help:
	@echo "Autotrader run targets:"
	@echo "  run-engine       Trade every active account (see .env's ACCOUNT_IDS/ACCOUNT_<id>_* - each account brings its own broker/mode/strategy)"
	@echo "  research         Screen run_research.py's symbol universe and update the watchlist (no trades placed)"
	@echo "  backend          Start the FastAPI backend API on $(BACKEND_HOST):$(BACKEND_PORT)"
	@echo "  dashboard        Start the React dashboard (UI) dev server - needs 'backend' running in another terminal"
	@echo "  restart          Background backend+dashboard+trading loop+research (bin/restart.sh)"
	@echo "                   Asks about each service one by one (Restart backend? [Y/n], etc.)"
	@echo "                   Pre-answer one via ARGS to skip its prompt, e.g.:"
	@echo "                     make restart ARGS=\"--skip-research\""
	@echo "                   Flags: --skip-backend --skip-dashboard --skip-engine --skip-research"
	@echo "  stop             Kill whatever 'restart' (or a previous manual run) started (bin/stop.sh)"
	@echo "                   Same per-service prompts/ARGS as restart"
	@echo "  restart-v2       Background backend-v2 (run_jobs.py) + dashboard-v2 (bin/restart-v2.sh)"
	@echo "                   Asks about each service one by one (Restart backend-v2? [Y/n], etc.)"
	@echo "                   Pre-answer one via ARGS to skip its prompt, e.g.:"
	@echo "                     make restart-v2 ARGS=\"--skip-backend\""
	@echo "                   Flags: --skip-backend --skip-dashboard"
	@echo "  stop-v2          Kill whatever 'restart-v2' (or a previous manual run) started (bin/stop-v2.sh)"
	@echo "                   Same per-service prompts/ARGS as restart-v2"
	@echo "  docker-build     Build the backend/engine and dashboard images"
	@echo "  docker-up        Start backend+engine+dashboard in containers (see docker-compose.yml)"
	@echo "  docker-down      Stop and remove the containers docker-up started"
	@echo "  docker-logs      Tail logs from all running containers"

run-engine:
	$(PYTHON) run_engine.py

research:
	$(PYTHON) run_research.py

backend:
	$(UVICORN) backend.app.main:app --host $(BACKEND_HOST) --port $(BACKEND_PORT)

dashboard:
	cd frontend && npm run dev

restart:
	@bin/restart.sh $(ARGS)

stop:
	@bin/stop.sh $(ARGS)

restart-v2:
	@bin/restart-v2.sh $(ARGS)

stop-v2:
	@bin/stop-v2.sh $(ARGS)

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f