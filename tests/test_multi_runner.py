import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Account, Base, Signal, SignalAction
from engine.brokers.base import AccountSnapshot, ClockSnapshot, OrderResult
from engine.multi_runner import REBALANCE_STRATEGY_NAME, _already_rebalanced_this_month, main, rebalance_account_now, run_all_accounts_once


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_no_prior_signal_returns_false():
    session = _session()
    assert _already_rebalanced_this_month(session, "acct-1") is False


def test_signal_earlier_this_month_returns_true():
    session = _session()
    session.add(
        Signal(account_id="acct-1", symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime(2026, 8, 1))
    )
    session.commit()
    assert _already_rebalanced_this_month(session, "acct-1", now=dt.datetime(2026, 8, 15)) is True


def test_signal_last_month_returns_false():
    session = _session()
    session.add(
        Signal(account_id="acct-1", symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime(2026, 7, 15))
    )
    session.commit()
    assert _already_rebalanced_this_month(session, "acct-1", now=dt.datetime(2026, 8, 1)) is False


def test_signal_from_a_different_account_does_not_count():
    """Each account's monthly-rebalance guard must be independent - one account
    rebalancing this month must never suppress another account's rebalance."""
    session = _session()
    session.add(
        Signal(account_id="other-acct", symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime(2026, 8, 1))
    )
    session.commit()
    assert _already_rebalanced_this_month(session, "acct-1", now=dt.datetime(2026, 8, 15)) is False


def test_main_validates_config_before_starting_scheduler(monkeypatch):
    """main() must resolve config before scheduler.start() - otherwise a bad config
    wouldn't surface until the next scheduled cron fire, and even then only as a logged
    APScheduler job error rather than a startup crash."""

    def boom(argv=None):
        raise RuntimeError("bad config")

    monkeypatch.setattr("engine.multi_runner.load_config", boom)

    with pytest.raises(RuntimeError, match="bad config"):
        main()


def test_run_all_accounts_once_continues_after_one_accounts_exception(monkeypatch):
    """One account's cycle raising must never prevent the others from running - see
    engine/multi_runner.py's run_all_accounts_once docstring."""
    ran_for = []

    class _FakeAccount:
        def __init__(self, id, strategy_name):
            self.id = id
            self.strategy_name = strategy_name
            self.strategy_params = "{}"
            self.pending_strategy_name = None

    accounts = [_FakeAccount("bad-acct", "ma_crossover"), _FakeAccount("good-acct", "ma_crossover")]

    monkeypatch.setattr("engine.multi_runner.sync_accounts_from_env", lambda session, config: None)
    monkeypatch.setattr("engine.multi_runner.get_active_accounts", lambda session: accounts)
    monkeypatch.setattr("engine.multi_runner.load_config", lambda argv=None: object())
    monkeypatch.setattr("engine.multi_runner.get_session", lambda: _session())
    monkeypatch.setattr("engine.multi_runner.make_notifier", lambda config: object())

    def fake_run_signal_account_once(account, strategy, config):
        if account.id == "bad-acct":
            raise RuntimeError("boom")
        ran_for.append(account.id)

    monkeypatch.setattr("engine.multi_runner._run_signal_account_once", fake_run_signal_account_once)
    monkeypatch.setattr("engine.multi_runner.log_and_notify", lambda *args, **kwargs: None)

    run_all_accounts_once(config=object())

    assert ran_for == ["good-acct"]


def test_run_all_accounts_once_applies_pending_strategy_change(monkeypatch):
    """A queued strategy change (backend/app/main.py's PATCH .../strategy, immediate=False)
    must be applied - and its pending fields cleared - before that cycle builds the
    account's strategy, matching what "takes effect the next trading cycle" promises."""
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    # expire_on_commit=False matches db/session.py's real SessionLocal - production code
    # (e.g. get_active_accounts' detached rows) relies on attributes staying readable after
    # the session that fetched them closes, so this test needs the same behavior to be faithful.
    SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)

    with SessionLocal() as session:
        session.add(
            Account(
                id="acct-1", broker="alpaca", display_name="Test", active=True,
                strategy_name="ma_crossover", strategy_params='{"short_window": 20, "long_window": 50}',
                pending_strategy_name="mean_reversion", pending_strategy_params='{"period": 14, "oversold": 30, "overbought": 70}',
                max_position_size_usd=1000.0, max_daily_loss_usd=200.0,
            )
        )
        session.commit()
        account = session.get(Account, "acct-1")
        session.expunge(account)  # detach - mirrors get_active_accounts returning rows from an already-closed session

    monkeypatch.setattr("engine.multi_runner.sync_accounts_from_env", lambda session, config: None)
    monkeypatch.setattr("engine.multi_runner.get_active_accounts", lambda session: [account])
    monkeypatch.setattr("engine.multi_runner.load_config", lambda argv=None: object())
    monkeypatch.setattr("engine.multi_runner.get_session", SessionLocal)

    used_strategy_names = []
    monkeypatch.setattr(
        "engine.multi_runner._run_signal_account_once",
        lambda account, strategy, config: used_strategy_names.append(account.strategy_name),
    )

    run_all_accounts_once(config=object())

    assert used_strategy_names == ["mean_reversion"]
    with SessionLocal() as session:
        fresh = session.get(Account, "acct-1")
        assert fresh.strategy_name == "mean_reversion"
        assert fresh.strategy_params == '{"period": 14, "oversold": 30, "overbought": 70}'
        assert fresh.pending_strategy_name is None
        assert fresh.pending_strategy_params is None


def test_rebalance_account_now_rejects_unknown_account(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr("engine.multi_runner.get_session", SessionLocal)

    with pytest.raises(ValueError, match="unknown account"):
        rebalance_account_now("nope", config=object())


def test_rebalance_account_now_rejects_non_rebalance_strategy(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr("engine.multi_runner.get_session", SessionLocal)

    with SessionLocal() as session:
        session.add(
            Account(
                id="acct-1", broker="alpaca", display_name="Test", active=True,
                strategy_name="ma_crossover", strategy_params='{"short_window": 20, "long_window": 50}',
                max_position_size_usd=1000.0, max_daily_loss_usd=200.0,
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="not running"):
        rebalance_account_now("acct-1", config=object())


def test_rebalance_account_now_forces_past_monthly_guard(monkeypatch):
    """The manual "Rebalance now" trigger must still act even if the account already
    rebalanced this month - that's the whole point of force=True (see
    backend/app/main.py's POST /accounts/{id}/rebalance)."""
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr("engine.multi_runner.get_session", SessionLocal)

    with SessionLocal() as session:
        session.add(
            Account(
                id="acct-1", broker="alpaca", display_name="Test", active=True,
                strategy_name=REBALANCE_STRATEGY_NAME, strategy_params='{"target_weights": {"SPY": 1.0}}',
                max_position_size_usd=100_000.0, max_daily_loss_usd=100_000.0, max_total_exposure_usd=0.0,
            )
        )
        # A rebalance signal already logged this month - simulates "already rebalanced".
        session.add(
            Signal(account_id="acct-1", symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime.utcnow())
        )
        session.commit()

    class FakeBroker:
        name = "alpaca"

        def get_account(self):
            return AccountSnapshot(equity=10_000.0, cash=10_000.0, buying_power=10_000.0, account_id="broker-acct-1")

        def get_clock(self):
            return ClockSnapshot(is_open=True)

        def get_positions(self):
            return []

        def get_bars(self, symbol, timeframe, limit=None, start=None):
            return pd.DataFrame({"close": [100.0]})

        def submit_market_order(self, symbol, action, qty):
            return OrderResult(broker_order_id="fake-order-1", status="filled")

    monkeypatch.setattr("engine.multi_runner.make_broker", lambda account_id, creds: FakeBroker())
    monkeypatch.setattr("engine.multi_runner.load_account_credentials", lambda account_id: object())
    monkeypatch.setattr("engine.multi_runner.make_notifier", lambda config: object())

    outcome = rebalance_account_now("acct-1", config=object())

    assert outcome == "rebalanced"
    with SessionLocal() as session:
        signals = session.execute(select(Signal).where(Signal.account_id == "acct-1")).scalars().all()
        assert len(signals) == 2  # the pre-existing "already rebalanced this month" one, plus the new forced one
