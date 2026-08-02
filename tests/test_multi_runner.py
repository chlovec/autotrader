import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Signal, SignalAction
from engine.multi_runner import REBALANCE_STRATEGY_NAME, _already_rebalanced_this_month, main, run_all_accounts_once


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
