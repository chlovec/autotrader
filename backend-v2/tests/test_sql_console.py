import csv
import datetime as dt
import io
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
import jobs.query_exports as query_exports
from app.main import app
from db.models import JobConfig, Ticker, TickerGroup
from db.session import SessionLocal, init_db
from jobs.query_exports import cleanup_query_exports
from jobs.registry import QUERY_EXPORT_CLEANUP_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TickerGroup).delete()
    session.query(Ticker).delete()
    session.query(JobConfig).filter(JobConfig.job_name == QUERY_EXPORT_CLEANUP_JOB).delete()
    session.add_all([Ticker(ticker=f"T{i:03d}", name=f"Ticker {i}", type="CS") for i in range(25)])
    session.commit()
    session.close()
    yield


@pytest.fixture(autouse=True)
def export_dir(tmp_path, monkeypatch):
    path = tmp_path / "query_exports"
    monkeypatch.setattr(query_exports, "EXPORT_DIR", path)
    monkeypatch.setattr(main, "EXPORT_DIR", path)
    return path


@pytest.fixture
def client():
    return TestClient(app)


def _export(client, sql, fmt):
    res = client.post("/admin/query/export", json={"sql": sql, "format": fmt})
    assert res.status_code == 200, res.text
    meta = res.json()
    download = client.get(f"/admin/query/exports/{meta['id']}")
    assert download.status_code == 200
    return meta, download


def test_query_reports_elapsed_ms(client):
    res = client.post("/admin/query", json={"sql": "SELECT ticker FROM tickers"})
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "rows"
    assert isinstance(body["elapsed_ms"], float) and body["elapsed_ms"] >= 0

    stmt = client.post("/admin/query", json={"sql": "UPDATE tickers SET name = name WHERE ticker = 'T000'"}).json()
    assert stmt["kind"] == "statement"
    assert stmt["elapsed_ms"] >= 0


def test_export_csv_ignores_console_row_cap(client, monkeypatch, export_dir):
    monkeypatch.setattr(main, "ADHOC_QUERY_MAX_ROWS", 10)
    monkeypatch.setattr(main, "ADHOC_EXPORT_CHUNK_ROWS", 7)

    shown = client.post("/admin/query", json={"sql": "SELECT ticker FROM tickers"}).json()
    assert shown["row_count"] == 10 and shown["truncated"] is True

    meta, download = _export(client, "SELECT ticker, name FROM tickers ORDER BY ticker", "csv")
    assert meta["row_count"] == 25
    assert meta["filename"].startswith("query-results-") and meta["filename"].endswith(".csv")
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(download.text)))
    assert rows[0] == ["ticker", "name"]
    assert len(rows) == 26
    assert rows[1] == ["T000", "Ticker 0"]
    # Saved on disk, no leftover .part file.
    assert [p.name for p in export_dir.iterdir()] == [f"{meta['id']}.csv"]
    assert meta["size_bytes"] == (export_dir / f"{meta['id']}.csv").stat().st_size


def test_export_json_respects_query_limit(client, monkeypatch):
    monkeypatch.setattr(main, "ADHOC_EXPORT_CHUNK_ROWS", 2)
    meta, download = _export(client, "SELECT ticker FROM tickers ORDER BY ticker LIMIT 5", "json")
    assert meta["row_count"] == 5
    assert download.json() == [{"ticker": f"T{i:03d}"} for i in range(5)]


def test_export_empty_result(client):
    sql = "SELECT ticker FROM tickers WHERE 0"
    assert _export(client, sql, "json")[1].json() == []
    assert _export(client, sql, "csv")[1].text.strip() == "ticker"


def test_export_delete_after_uses_job_max_age(client):
    before = dt.datetime.now(dt.timezone.utc)
    meta, _ = _export(client, "SELECT 1 AS x", "csv")
    default_delay = dt.datetime.fromisoformat(meta["delete_after"]) - before
    assert dt.timedelta(hours=2) <= default_delay < dt.timedelta(hours=2, minutes=1)

    with SessionLocal() as session:
        session.add(JobConfig(job_name=QUERY_EXPORT_CLEANUP_JOB, query_export_max_age_hours=0.5))
        session.commit()
    meta, _ = _export(client, "SELECT 1 AS x", "csv")
    delay = dt.datetime.fromisoformat(meta["delete_after"]) - before
    assert dt.timedelta(minutes=30) <= delay < dt.timedelta(minutes=31)


def test_export_refuses_writes_and_resets_connection(client, export_dir):
    res = client.post("/admin/query/export", json={"sql": "DELETE FROM tickers", "format": "csv"})
    assert res.status_code == 400
    assert not export_dir.exists() or not any(export_dir.iterdir())
    assert client.post("/admin/query", json={"sql": "SELECT COUNT(*) AS n FROM tickers"}).json()["rows"] == [
        {"n": 25}
    ]

    # The pooled connection must not stay query_only after an export.
    _export(client, "SELECT 1 AS x", "csv")
    upd = client.post("/admin/query", json={"sql": "UPDATE tickers SET name = 'x' WHERE ticker = 'T000'"})
    assert upd.status_code == 200 and upd.json()["rowcount"] == 1


def test_export_sql_error_is_400(client):
    res = client.post("/admin/query/export", json={"sql": "SELECT * FROM no_such_table", "format": "csv"})
    assert res.status_code == 400
    assert "no_such_table" in res.json()["detail"]


def test_download_unknown_or_invalid_id_is_404(client, export_dir):
    export_dir.mkdir(parents=True)
    (export_dir / "secret.txt").write_text("nope")
    assert client.get(f"/admin/query/exports/{'0' * 32}").status_code == 404
    assert client.get("/admin/query/exports/secret").status_code == 404
    assert client.get("/admin/query/exports/..%2Fsecret.txt").status_code == 404


def _touch(path, age_hours, now):
    path.write_text("x")
    stamp = now.timestamp() - age_hours * 3600
    os.utime(path, (stamp, stamp))


def test_cleanup_deletes_only_old_export_files(export_dir):
    export_dir.mkdir(parents=True)
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
    old_csv = export_dir / f"{'a' * 32}.csv"
    old_part = export_dir / f"{'b' * 32}.json.part"
    fresh_json = export_dir / f"{'c' * 32}.json"
    unrelated = export_dir / "notes.txt"
    _touch(old_csv, 2, now)
    _touch(old_part, 3, now)
    _touch(fresh_json, 1.5, now)
    _touch(unrelated, 100, now)

    assert cleanup_query_exports(2, now=now) == 2
    assert sorted(p.name for p in export_dir.iterdir()) == sorted([fresh_json.name, unrelated.name])

    assert cleanup_query_exports(1, now=now) == 1
    assert [p.name for p in export_dir.iterdir()] == [unrelated.name]


def test_cleanup_missing_dir_is_noop(export_dir):
    assert cleanup_query_exports(2) == 0


def test_job_config_max_age_round_trip_and_validation(client):
    job = client.get(f"/jobs/{QUERY_EXPORT_CLEANUP_JOB}").json()
    assert job["has_query_export_cleanup_fields"] is True
    assert job["query_export_max_age_hours"] is None
    assert job["run_type"] == "auto"

    base = {
        "run_type": job["run_type"],
        "schedule_interval_unit": job["schedule_interval_unit"],
        "schedule_interval_value": job["schedule_interval_value"],
        "start_time": job["start_time"],
    }
    saved = client.put(f"/jobs/{QUERY_EXPORT_CLEANUP_JOB}/config", json={**base, "query_export_max_age_hours": 6})
    assert saved.status_code == 200
    assert saved.json()["query_export_max_age_hours"] == 6

    bad = client.put(f"/jobs/{QUERY_EXPORT_CLEANUP_JOB}/config", json={**base, "query_export_max_age_hours": 0})
    assert bad.status_code == 400

    # Other jobs drop the field even if it's sent.
    other = client.get("/jobs/sync-tickers").json()
    other_base = {k: other[k] for k in base}
    client.put("/jobs/sync-tickers/config", json={**other_base, "query_export_max_age_hours": 6})
    assert client.get("/jobs/sync-tickers").json()["query_export_max_age_hours"] is None


# Never finishes on its own within any sane test timeout - only a cancel ends it.
ENDLESS_SQL = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
    "SELECT COUNT(*) FROM n"
)


def _run_and_cancel(client, path: str, body: dict):
    """Starts `body` on `path` in a thread, cancels it once it's registered as running,
    and returns the query's own response."""
    response = {}
    thread = threading.Thread(target=lambda: response.setdefault("r", client.post(path, json=body)))
    thread.start()
    deadline = time.monotonic() + 10
    while body["query_id"] not in main._running_queries:
        assert time.monotonic() < deadline, "query never started"
        time.sleep(0.01)
    assert client.post(f"/admin/query/{body['query_id']}/cancel").json() == {"cancelled": True}
    thread.join(timeout=10)
    assert not thread.is_alive(), "query wasn't interrupted"
    return response["r"]


def test_cancel_unknown_query_is_noop(client):
    assert client.post("/admin/query/nope/cancel").json() == {"cancelled": False}


def test_cancel_running_query(client):
    res = _run_and_cancel(client, "/admin/query", {"sql": ENDLESS_SQL, "query_id": "q1"})
    assert res.status_code == 409
    assert res.json()["detail"] == main.ADHOC_QUERY_CANCELLED_DETAIL
    assert main._running_queries == {} and main._cancelled_queries == set()
    # The interrupt doesn't stick to the pooled connection - the next query runs fine.
    assert client.post("/admin/query", json={"sql": "SELECT COUNT(*) AS n FROM tickers"}).json()["rows"] == [
        {"n": 25}
    ]


def test_cancel_running_export_leaves_no_file(client, export_dir):
    res = _run_and_cancel(
        client, "/admin/query/export", {"sql": ENDLESS_SQL, "format": "csv", "query_id": "e1"}
    )
    assert res.status_code == 409
    assert not export_dir.exists() or list(export_dir.iterdir()) == []
    assert client.post("/admin/query/export", json={"sql": "SELECT 1 AS x", "format": "csv"}).status_code == 200
