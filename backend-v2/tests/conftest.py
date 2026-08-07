"""Pytest always imports conftest.py before collecting any test module in this
directory - this has to be the thing that redirects BACKEND_V2_DATABASE_URL, not each
test file individually.

Without this, test_client.py's `from data.client import DataClient` is the first thing
pytest collects, and data/client.py's module-level load_dotenv() call fills
BACKEND_V2_DATABASE_URL from backend-v2/.env (the real sqlite:///./backend_v2.db)
before any other test file gets a chance to set it first. db/session.py's engine is a
module-level singleton bound on its first import, so whichever test file imports it
first locks that URL in for the rest of the pytest process - which used to be the real
database, with every test file's DELETE-heavy `_clean_db` fixture then running against
it instead of a throwaway one.
"""

import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["BACKEND_V2_DATABASE_URL"] = f"sqlite:///{_db_path}"
