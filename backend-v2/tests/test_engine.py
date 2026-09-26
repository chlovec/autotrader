import datetime as dt
import json

import pytest

from db.models import JobConfig, JobRun
from db.session import SessionLocal, init_db
from jobs.engine import _resolve_prediction_date, apply_run_overrides, reconcile_orphaned_runs
from jobs.predict_market_state import DEFAULT_PREDICTED_DATE_OFFSET_DAYS
from jobs.registry import OHLC_UPDATE_JOB, PREDICT_MARKET_STATE_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(JobRun).delete()
    session.query(JobConfig).delete()
    session.commit()
    session.close()
    yield


def test_reconcile_marks_in_progress_runs_as_failed():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="in_progress",
                started_at=dt.datetime.utcnow(),
                progress_completed=2250,
                progress_total=37216,
            )
        )
        session.commit()

        reconciled = reconcile_orphaned_runs()
        assert reconciled == 1

        run = session.query(JobRun).filter_by(job_name=PREDICT_MARKET_STATE_JOB).one()
        assert run.status == "failed"
        assert run.error is not None
        assert run.finished_at is not None
        # Progress from the orphaned attempt is left as-is - it's historical context for
        # why the run failed, not something reconcile_orphaned_runs needs to touch.
        assert run.progress_completed == 2250
    finally:
        session.close()


def test_reconcile_leaves_completed_and_failed_runs_alone():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="completed",
                started_at=dt.datetime.utcnow(),
                finished_at=dt.datetime.utcnow(),
                result_summary="0 ticker(s) market state predicted",
            )
        )
        session.commit()

        reconciled = reconcile_orphaned_runs()
        assert reconciled == 0

        run = session.query(JobRun).filter_by(job_name=PREDICT_MARKET_STATE_JOB).one()
        assert run.status == "completed"
    finally:
        session.close()


def test_reconcile_is_a_noop_with_no_runs():
    assert reconcile_orphaned_runs() == 0


def test_apply_run_overrides_is_a_noop_when_nothing_pending():
    session = SessionLocal()
    try:
        config = JobConfig(
            job_name=OHLC_UPDATE_JOB,
            run_type="manual",
            schedule_interval_unit="days",
            schedule_interval_value=1,
            ticker_types="ETF,ETN,ETS,ETV",
        )
        session.add(config)
        session.commit()

        apply_run_overrides(session, OHLC_UPDATE_JOB, config)

        assert config.ticker_types == "ETF,ETN,ETS,ETV"
    finally:
        session.close()


def test_apply_run_overrides_overrides_in_memory_without_persisting():
    """The whole point of run_overrides: a manual run with a blank ticker_types filter
    must not be silently scoped down to whatever's still saved in JobConfig (the
    reported bug this feature fixes), and must not overwrite that saved value either -
    it's a one-time override for this run only."""
    session = SessionLocal()
    try:
        config = JobConfig(
            job_name=OHLC_UPDATE_JOB,
            run_type="manual",
            schedule_interval_unit="days",
            schedule_interval_value=1,
            ticker_types="ETF,ETN,ETS,ETV",
            ohlc_update_start_date=dt.date(2026, 1, 1),
            ohlc_update_end_date=dt.date(2026, 1, 31),
            run_overrides=json.dumps(
                {
                    "ticker_types": None,
                    "tickers": None,
                    "ohlc_update_start_date": "2026-06-01",
                    "ohlc_update_end_date": "2026-06-30",
                }
            ),
        )
        session.add(config)
        session.commit()

        apply_run_overrides(session, OHLC_UPDATE_JOB, config)

        # In-memory: this run sees the override, with dates parsed back to dt.date.
        assert config.ticker_types is None
        assert config.ohlc_update_start_date == dt.date(2026, 6, 1)
        assert config.ohlc_update_end_date == dt.date(2026, 6, 30)
        # Expunged, so nothing later in this session can flush these changes back.
        assert config not in session

        # Persisted row: untouched apart from run_overrides itself being cleared -
        # the saved filter/schedule survives exactly as it was before this run.
        session.expire_all()
        persisted = session.get(JobConfig, OHLC_UPDATE_JOB)
        assert persisted.ticker_types == "ETF,ETN,ETS,ETV"
        assert persisted.ohlc_update_start_date == dt.date(2026, 1, 1)
        assert persisted.ohlc_update_end_date == dt.date(2026, 1, 31)
        assert persisted.run_overrides is None
    finally:
        session.close()


def _minimal_config(**kwargs) -> JobConfig:
    return JobConfig(
        job_name=PREDICT_MARKET_STATE_JOB,
        run_type="manual",
        schedule_interval_unit="days",
        schedule_interval_value=1,
        **kwargs,
    )


def test_resolve_prediction_date_auto_trigger_always_uses_offset():
    """A scheduled/automatic run must use predicted_date_offset_days even if a manual
    run earlier left a literal prediction_start_date saved on this same config row -
    the whole point of resolving by trigger rather than by "whichever field is set"."""
    today = dt.datetime.now(dt.timezone.utc).date()
    config = _minimal_config(predicted_date_offset_days=3, prediction_start_date=dt.date(2020, 1, 1))

    assert _resolve_prediction_date(config, "auto") == today + dt.timedelta(days=3)


def test_resolve_prediction_date_manual_trigger_uses_picked_date():
    config = _minimal_config(prediction_start_date=dt.date(2026, 6, 15), predicted_date_offset_days=5)

    assert _resolve_prediction_date(config, "manual") == dt.date(2026, 6, 15)


def test_resolve_prediction_date_manual_trigger_without_picked_date_falls_back_to_offset():
    today = dt.datetime.now(dt.timezone.utc).date()
    config = _minimal_config(predicted_date_offset_days=2, prediction_start_date=None)

    assert _resolve_prediction_date(config, "manual") == today + dt.timedelta(days=2)


def test_resolve_prediction_date_defaults_to_tomorrow_when_nothing_set():
    today = dt.datetime.now(dt.timezone.utc).date()
    config = _minimal_config()

    assert _resolve_prediction_date(config, "auto") == today + dt.timedelta(days=DEFAULT_PREDICTED_DATE_OFFSET_DAYS)
