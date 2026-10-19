import datetime as dt

import app.main as main_module

import pytest
from fastapi.testclient import TestClient

from app.main import app
from db import models
from db.models import CurrentPredResult, Ticker, TickerHitRate, TickerPredictionHeader
from db.session import SessionLocal, init_db

RESULT_HEADER = (
    '"","ticker","timestamp","close","prev_timestamp","prev_close","pct_return","p_end","p_start",'
    '"momentum","volatility","date","pcnt_daily_return","momentum_z","volatility_z","pred",'
    '"pred_close","pcnt_diff"'
)
HIT_RATE_HEADER = '"Direction","Ticker","Count of Trading Days","Hit Rate (Count)","Hit Rate (Pcnt)","Pcnt Difference"'


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    for model in (TickerHitRate, models.TestPredResult, CurrentPredResult, TickerPredictionHeader, Ticker):
        session.query(model).delete()
    session.add_all(
        [
            Ticker(ticker="AAPL", name="Apple Inc."),
            Ticker(ticker="MSFT", name="Microsoft Corp"),
            Ticker(ticker="NA", name="Nano Labs Ltd"),
            Ticker(ticker="ABRpD", name="Arbor Realty Trust 6.375% Series D Preferred"),
            Ticker(ticker="NAN", name="Nuveen New York Quality Municipal Income Fund"),
        ]
    )
    session.commit()
    session.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _create_header(client, **overrides) -> int:
    body = {"pred_date": "2026-10-16", "comments": "first", **overrides}
    response = client.post("/prediction-headers", json=body)
    assert response.status_code == 200, response.text
    return response.json()["pred_id"]


def test_create_list_and_update_header(client):
    first = _create_header(client)
    second = _create_header(client, pred_date="2026-10-17", comments="  ", ref_pred_id=first)

    rows = client.get("/prediction-headers").json()
    assert [r["pred_id"] for r in rows] == [second, first]
    assert rows[0]["comments"] is None
    assert rows[0]["ref_pred_id"] == first

    updated = client.put(f"/prediction-headers/{first}", json={"pred_date": "2026-10-15", "comments": "edited"})
    assert updated.status_code == 200
    assert updated.json()["pred_date"] == "2026-10-15"
    assert updated.json()["comments"] == "edited"


def test_header_ref_validation(client):
    first = _create_header(client)
    assert client.post("/prediction-headers", json={"pred_date": "2026-10-16", "ref_pred_id": 999}).status_code == 400
    assert client.put(f"/prediction-headers/{first}", json={"pred_date": "2026-10-16", "ref_pred_id": first}).status_code == 400
    assert client.put("/prediction-headers/999", json={"pred_date": "2026-10-16"}).status_code == 404


def test_upload_hit_rates_upserts_and_replaces(client):
    pred_id = _create_header(client)
    csv_text = f"{HIT_RATE_HEADER}\nUp,AAPL,20,12,60,1.5\nDown,MSFT,20,9,45,NA\n"
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=csv_text)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 2

    with SessionLocal() as session:
        aapl = session.get(TickerHitRate, (pred_id, "AAPL", "Up"))
        assert (aapl.direction, aapl.trading_days_count, aapl.hit_rate_count, aapl.hit_rate_pcnt) == ("Up", 20, 12, 60.0)
        assert session.get(TickerHitRate, (pred_id, "MSFT", "Down")).pcnt_difference is None

    # Upsert keeps MSFT; replace drops it.
    client.post(f"/prediction-headers/{pred_id}/hit-rates", content=f"{HIT_RATE_HEADER}\nUp,AAPL,21,13,61.9,2\n")
    assert client.get("/prediction-headers").json()[0]["hit_rate_rows"] == 2
    response = client.post(
        f"/prediction-headers/{pred_id}/hit-rates",
        content=f"{HIT_RATE_HEADER}\nUp,AAPL,21,13,61.9,2\n", params={"replace": "true"},
    )
    assert response.json()["replaced"] == 2
    assert client.get("/prediction-headers").json()[0]["hit_rate_rows"] == 1


def test_upload_hit_rates_keeps_both_directions_per_ticker(client):
    pred_id = _create_header(client)
    csv_text = f"{HIT_RATE_HEADER}\nUp,AAPL,20,12,60,1.5\nDown,AAPL,20,7,35,-2\n"
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=csv_text)
    assert response.status_code == 200, response.text

    with SessionLocal() as session:
        assert session.get(TickerHitRate, (pred_id, "AAPL", "Up")).hit_rate_pcnt == 60.0
        assert session.get(TickerHitRate, (pred_id, "AAPL", "Down")).hit_rate_pcnt == 35.0
    assert client.get("/prediction-headers").json()[0]["hit_rate_rows"] == 2


def test_upload_rejects_missing_columns_bad_values_and_unknown_tickers(client):
    pred_id = _create_header(client)
    url = f"/prediction-headers/{pred_id}/hit-rates"

    missing = client.post(url, content="Direction,Ticker\nUp,AAPL\n")
    assert missing.status_code == 400
    assert "Count of Trading Days" in missing.json()["detail"]

    bad = client.post(url, content=f"{HIT_RATE_HEADER}\nUp,AAPL,twenty,12,60,1\n")
    assert bad.status_code == 400
    assert "line 2" in bad.json()["detail"]

    unknown = client.post(url, content=f"{HIT_RATE_HEADER}\nUp,NOPE,20,12,60,1\n")
    assert unknown.status_code == 400
    assert "NOPE" in unknown.json()["detail"]

    assert client.post("/prediction-headers/999/hit-rates", content=HIT_RATE_HEADER).status_code == 404
    with SessionLocal() as session:
        assert session.query(TickerHitRate).count() == 0


@pytest.mark.parametrize("kind, model", [("test", models.TestPredResult), ("current", CurrentPredResult)])
def test_upload_results(client, kind, model):
    pred_id = _create_header(client)
    csv_text = (
        f"{RESULT_HEADER}\n"
        '"1","AAPL","2026-10-16 00:00:00",230.5,"2026-10-15",228,0.0096,230.5,220,0.04,0.02,"2026-10-16",'
        "0.96,1.2,-0.3,0.01,232.8,1.0\n"
        '"2","AAPL","2026-10-17",NA,NA,NA,NA,NA,NA,NA,NA,NA,NA,NA,NA,0.02,235,NA\n'
    )
    response = client.post(f"/prediction-headers/{pred_id}/results/{kind}", content=csv_text)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 2

    with SessionLocal() as session:
        first = session.get(model, (pred_id, "AAPL", dt.datetime(2026, 10, 16)))
        assert first.prev_timestamp == dt.datetime(2026, 10, 15)
        assert first.date == dt.date(2026, 10, 16)
        assert first.pred_close == 232.8
        second = session.get(model, (pred_id, "AAPL", dt.datetime(2026, 10, 17)))
        assert second.close is None
        assert second.pred_close == 235.0


def test_upload_results_unknown_kind_is_422(client):
    pred_id = _create_header(client)
    assert client.post(f"/prediction-headers/{pred_id}/results/other", content=RESULT_HEADER).status_code == 422


def test_upload_rejects_files_over_the_limit(client, monkeypatch):
    pred_id = _create_header(client)
    monkeypatch.setattr(main_module, "MAX_PREDICTION_UPLOAD_BYTES", 50)
    response = client.post(
        f"/prediction-headers/{pred_id}/hit-rates", content=f"{HIT_RATE_HEADER}\nUp,AAPL,20,12,60,1.5\n"
    )
    assert response.status_code == 413
    with SessionLocal() as session:
        assert session.query(TickerHitRate).count() == 0


def test_upload_streams_rows_across_multiple_batches(client, monkeypatch):
    pred_id = _create_header(client)
    monkeypatch.setattr(main_module, "_CSV_INSERT_BATCH", 7)
    start = dt.datetime(2026, 1, 1)
    lines = [RESULT_HEADER] + [
        f'"{i}","MSFT","{(start + dt.timedelta(days=i)).isoformat(sep=" ")}",{100 + i}' + ",NA" * 14
        for i in range(50)
    ]
    response = client.post(f"/prediction-headers/{pred_id}/results/test", content="\n".join(lines) + "\n")
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 50
    assert client.get("/prediction-headers").json()[0]["test_result_rows"] == 50


def test_bad_row_late_in_file_writes_nothing(client):
    pred_id = _create_header(client)
    good = [f"Up,AAPL,20,12,60,{i}" for i in range(20)]
    csv_text = "\n".join([HIT_RATE_HEADER, *good, "Up,MSFT,x,12,60,1"]) + "\n"
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=csv_text)
    assert response.status_code == 400
    assert "line 22" in response.json()["detail"]
    with SessionLocal() as session:
        assert session.query(TickerHitRate).count() == 0


def test_na_and_nan_are_tickers_not_missing_values(client):
    pred_id = _create_header(client)
    csv_text = f'{HIT_RATE_HEADER}\nUp,NA,20,12,60,NA\n"Down","NAN",20,9,45,1\nUp,"",20,9,45,1\n'
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=csv_text)
    # The blank ticker on line 4 is still rejected...
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("line 4: Ticker is required - row: ")
    assert "Ticker=''" in detail

    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content="\n".join(csv_text.splitlines()[:3]))
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        # ...while NA/NAN load as tickers, and NA elsewhere is still a missing value.
        assert session.get(TickerHitRate, (pred_id, "NA", "Up")).pcnt_difference is None
        assert session.get(TickerHitRate, (pred_id, "NAN", "Down")) is not None


def test_error_shows_the_bad_rows_values_and_short_rows(client):
    pred_id = _create_header(client)
    csv_text = f"{HIT_RATE_HEADER}\nUp,AAPL,20,12,60,1\nDown,,20,9,45,1\nUp,MSFT\n"
    detail = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=csv_text).json()["detail"]
    first, second = detail.split("\n")
    assert first == (
        "line 3: Ticker is required - row: Direction='Down', Ticker='', Count of Trading Days='20', "
        "Hit Rate (Count)='9', Hit Rate (Pcnt)='45', Pcnt Difference='1'"
    )
    assert second.startswith("line 4: Count of Trading Days is required - row: Direction='Up', Ticker='MSFT'")
    assert second.endswith("(only 2 of 6 values - row is short)")


def test_ticker_case_is_kept_for_preferred_shares(client):
    pred_id = _create_header(client)
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=f"{HIT_RATE_HEADER}\nUp,ABRpD,20,12,60,1\n")
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        assert session.get(TickerHitRate, (pred_id, "ABRpD", "Up")) is not None

    # A different case is a different (unknown) ticker, not silently matched.
    response = client.post(f"/prediction-headers/{pred_id}/hit-rates", content=f"{HIT_RATE_HEADER}\nUp,ABRPD,20,12,60,1\n")
    assert response.status_code == 400
    assert "ABRPD" in response.json()["detail"]
