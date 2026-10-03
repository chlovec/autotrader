import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from db.models import Ticker, TickerGroup
from db.session import SessionLocal, init_db


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TickerGroup).delete()
    session.query(Ticker).delete()
    session.add_all(
        [
            Ticker(ticker="AAPL", name="Apple Inc.", type="CS", primary_exchange="XNAS"),
            Ticker(ticker="MSFT", name="Microsoft Corp", type="CS", primary_exchange="XNAS"),
        ]
    )
    session.commit()
    session.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_add_list_and_remove(client):
    assert client.get("/watchlist").json() == []

    added = client.post("/watchlist", json={"ticker": " aapl "})
    assert added.status_code == 200
    assert added.json()["ticker"] == "AAPL"
    assert added.json()["name"] == "Apple Inc."

    rows = client.get("/watchlist").json()
    assert [r["ticker"] for r in rows] == ["AAPL"]

    assert client.delete("/watchlist/aapl").status_code == 200
    assert client.get("/watchlist").json() == []


def test_add_unknown_ticker_is_404(client):
    assert client.post("/watchlist", json={"ticker": "NOPE"}).status_code == 404


def test_add_duplicate_is_409(client):
    client.post("/watchlist", json={"ticker": "MSFT"})
    assert client.post("/watchlist", json={"ticker": "MSFT"}).status_code == 409


def test_remove_missing_is_404(client):
    assert client.delete("/watchlist/MSFT").status_code == 404


def test_watchlist_ignores_other_groups(client):
    session = SessionLocal()
    session.add(TickerGroup(ticker="MSFT", group="core_holdings", created_at=dt.datetime(2026, 1, 1)))
    session.commit()
    session.close()
    assert client.get("/watchlist").json() == []
    assert client.delete("/watchlist/MSFT").status_code == 404


def test_new_tickers_go_to_top(client):
    client.post("/watchlist", json={"ticker": "AAPL"})
    client.post("/watchlist", json={"ticker": "MSFT"})
    assert [r["ticker"] for r in client.get("/watchlist").json()] == ["MSFT", "AAPL"]


def test_reorder_persists(client):
    client.post("/watchlist", json={"ticker": "AAPL"})
    client.post("/watchlist", json={"ticker": "MSFT"})
    response = client.post("/watchlist/reorder", json={"tickers": ["aapl", "MSFT"]})
    assert response.status_code == 200
    assert [r["ticker"] for r in response.json()] == ["AAPL", "MSFT"]
    assert [r["ticker"] for r in client.get("/watchlist").json()] == ["AAPL", "MSFT"]


@pytest.mark.parametrize("tickers", [["AAPL"], ["AAPL", "MSFT", "GOOG"], ["AAPL", "AAPL"]])
def test_reorder_rejects_mismatched_list(client, tickers):
    client.post("/watchlist", json={"ticker": "AAPL"})
    client.post("/watchlist", json={"ticker": "MSFT"})
    assert client.post("/watchlist/reorder", json={"tickers": tickers}).status_code == 409
