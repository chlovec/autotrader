"""Launcher for backend-v2's API + scheduled-jobs process. The actual app - job
endpoints, CORS, and the lifespan handler that runs sync-tickers/sync-bars-nightly once
on startup then keeps them on a cron schedule - lives in app/main.py. Kept as a
separate script (rather than invoking `uvicorn app.main:app` directly) so
bin/restart-v2.sh's `python run_jobs.py` invocation doesn't need to change.

Usage (from backend-v2/, matching how bin/restart-v2.sh runs it):
    python run_jobs.py

Env: BACKEND_V2_HOST (default 127.0.0.1), BACKEND_V2_PORT (default 8001), plus
everything app/main.py itself reads (CORS_ORIGINS, MASSIVE_API_KEY, ...).
"""

import logging
import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    host = os.environ.get("BACKEND_V2_HOST", "127.0.0.1")
    port = int(os.environ.get("BACKEND_V2_PORT", "8001"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
