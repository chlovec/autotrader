"""On-disk files behind the SQL console's Export CSV/JSON buttons, and the
cleanup-query-exports job that deletes them once they're old enough.

app/main.py's export_adhoc_query writes each export here (via a ".part" file renamed
into place once complete, so a half-written file is never served), and
download_adhoc_query_export serves it back by id. This job runs in job_runner.py's
separate process, so the two only share this directory - nothing about an export is
stored in the database.
"""

import datetime as dt
import re
import uuid
from pathlib import Path

from jobs.control import JobControl

EXPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "query_exports"

# Used when JobConfig.query_export_max_age_hours is NULL - see db/models.py.
DEFAULT_MAX_AGE_HOURS = 2.0

EXPORT_FORMATS = ("csv", "json")

# <32 hex uuid>.<format>, optionally with a trailing .part while still being written.
# Both the download endpoint and the cleanup job only ever touch names matching this,
# so nothing else that ends up in EXPORT_DIR can be served or deleted by either.
_EXPORT_NAME = re.compile(r"^([0-9a-f]{32})\.(csv|json)(\.part)?$")


def new_export_id() -> str:
    return uuid.uuid4().hex


def export_path(export_id: str, fmt: str) -> Path:
    return EXPORT_DIR / f"{export_id}.{fmt}"


def find_export(export_id: str) -> Path | None:
    """The finished export file for `export_id`, or None if it doesn't exist (never
    created, still being written, or already cleaned up)."""
    if not re.fullmatch(r"[0-9a-f]{32}", export_id):
        return None
    for fmt in EXPORT_FORMATS:
        path = export_path(export_id, fmt)
        if path.is_file():
            return path
    return None


def cleanup_query_exports(
    max_age_hours: float,
    *,
    now: dt.datetime | None = None,
    control: JobControl | None = None,
) -> int:
    """Deletes every export file (finished or a leftover .part) in EXPORT_DIR whose
    last-modified time is at least `max_age_hours` old. Returns how many were deleted."""
    if not EXPORT_DIR.is_dir():
        return 0
    cutoff = (now or dt.datetime.now(dt.timezone.utc)).timestamp() - max_age_hours * 3600
    deleted = 0
    for path in EXPORT_DIR.iterdir():
        if control is not None:
            control.checkpoint_sync()
        if not _EXPORT_NAME.match(path.name) or not path.is_file():
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            # Deleted by something else between iterdir() and here - nothing to do.
            continue
    return deleted
