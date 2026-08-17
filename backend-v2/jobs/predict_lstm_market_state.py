"""Runs a trained jobs/lstm_common.LstmModel (jobs/train_lstm_holdout.py or
jobs/train_lstm_walkforward.py's output) over each selected ticker's most recent
lookback window and upserts a next-session state prediction - full softmax state
probabilities, expected return, and a projected entry/exit price - into the
lstm_inferences table. The LSTM counterpart to jobs/predict_market_state.py's Markov
chain and jobs/predict_market_state_mcmc.py's Monte Carlo simulation; all three target
the same STATE_LABELS buckets, so they're directly comparable on the Prediction
Comparison report (see app/main.py's prediction_comparison_report).

Called by two independent jobs - predict-lstm-market-state-holdout and
predict-lstm-market-state-walkforward (jobs/registry.py's LSTM_INFERENCE_TRAINING_METHODS,
dispatched in jobs/engine.py's run_job) - each fixing `training_method` to its own
flavor, so a re-run of one can never overwrite the other's row for the same (ticker,
predicted_date): the whole point of keeping them independent is comparing the two
flavors' live predictions side by side, which a single "whichever model is newest" job
couldn't guarantee.

Purely local aggregation over already-synced bars plus a saved checkpoint - no
massive.com call involved, same reasoning as jobs/predict_market_state.py.
"""

import datetime as dt
import logging

import torch
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import LstmInference, LstmModelVersion
from jobs.control import JobControl, report_job_progress
from jobs.lstm_common import STATE_LABELS, build_inference_windows, load_checkpoint, select_device
from jobs.predict_market_state import DEFAULT_PREDICTED_DATE_OFFSET_DAYS, ENTRY_TIME, EXIT_TIME

logger = logging.getLogger("backend_v2.jobs.predict_lstm_market_state")

# Commits every this-many tickers instead of once at the very end - same reasoning as
# jobs/predict_market_state.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500


def _resolve_model_version(session: Session, training_method: str, model_version_id: int | None) -> LstmModelVersion:
    if model_version_id is not None:
        version = session.get(LstmModelVersion, model_version_id)
        if version is None:
            raise ValueError(f"no LstmModelVersion with id {model_version_id}")
        if version.training_method != training_method:
            raise ValueError(
                f"LstmModelVersion {model_version_id} was trained with training_method="
                f"{version.training_method!r}, not {training_method!r}"
            )
        return version
    version = session.execute(
        select(LstmModelVersion)
        .where(LstmModelVersion.training_method == training_method)
        .order_by(LstmModelVersion.trained_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        raise ValueError(f"no trained {training_method!r} LSTM model available - run train-lstm-{training_method} first")
    return version


def compute_lstm_market_state_predictions(
    session: Session,
    training_method: str,
    prediction_date: dt.date | None = None,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    model_version_id: int | None = None,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> int:
    """`training_method` ("holdout" or "walkforward") is which flavor of trained model
    to run - always fixed per caller (see this module's docstring), never left to
    "whichever is newest" the way an unscoped single inference job would have to.

    `prediction_date` defaults to tomorrow (UTC), same default
    jobs/predict_market_state.py's own compute_market_state_predictions uses. Uses the
    LstmModelVersion given by `model_version_id` (which must itself be of
    `training_method`), or the most recently trained one of that training_method if
    left unset - see db/models.py's JobConfig.lstm_model_version_id docstring.

    Runs off the event loop (see jobs/engine.py's run_job) - control.checkpoint_sync is
    called once per ticker while upserting results, same granularity as
    jobs/predict_market_state.py. `run_id`, if given, reports (ticker index / ticker
    count) progress the same way via jobs/control.report_job_progress - unthrottled
    here (the normal per-_PROGRESS_COMMIT_INTERVAL throttle still applies, this is a
    genuine tens-of-thousands-strong per-ticker fan-out, unlike the two training jobs'
    single-digit epoch counts).

    Builds every qualifying ticker's most recent lookback window
    (jobs/lstm_common.build_inference_windows, using only bars strictly before
    prediction_date) and runs one batched forward pass over all of them, on
    jobs/lstm_common.select_device's best available device (benchmarked ~2.5-3.5x
    faster than CPU for this single large batch - see that function's docstring), then
    upserts a prediction per ticker into lstm_inferences, keyed by (ticker,
    predicted_date, training_method) - see LstmInference's docstring for why
    training_method is part of its primary key.

    Returns the number of tickers a prediction was stored for."""
    if prediction_date is None:
        prediction_date = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(
            days=DEFAULT_PREDICTED_DATE_OFFSET_DAYS
        )

    device = select_device()
    version = _resolve_model_version(session, training_method, model_version_id)
    model = load_checkpoint(version.model_path).to(device)

    windows = build_inference_windows(
        session,
        prediction_date,
        lookback_days=version.lookback_days,
        ticker_types=ticker_types,
        tickers=tickers,
    )
    if not windows:
        logger.info("no tickers had enough history for an LSTM prediction targeting %s", prediction_date)
        return 0

    with torch.no_grad():
        features = torch.stack([window.features for window in windows]).to(device)
        class_logits, regression_pred = model(features)
        probabilities = torch.softmax(class_logits, dim=1).cpu()
        regression_pred = regression_pred.cpu()

    computed_at = dt.datetime.utcnow()
    stored = 0
    for i, window in enumerate(windows):
        if control is not None:
            control.checkpoint_sync()
        report_job_progress(session, run_id, i + 1, len(windows))

        probs = probabilities[i].tolist()
        predicted_index = max(range(len(STATE_LABELS)), key=lambda s: probs[s])
        expected_return = regression_pred[i].item()
        exit_price = window.entry_price * (1 + expected_return)
        # Same coefficient-of-variation formula shape as
        # jobs/predict_market_state.py's own exit_price_confidence, using this ticker's
        # own historical bucket_stds (jobs/lstm_common.build_inference_windows) for the
        # predicted state rather than state_confidence - a measure of price-spread
        # tightness, distinct from the classifier's confidence in *which* state was
        # predicted. 0.0 rather than a ZeroDivisionError on the (extreme, but not
        # impossible) expected_return == -1 case.
        exit_price_confidence = (
            1 - (window.bucket_stds[predicted_index] / (1 + expected_return)) if expected_return != -1 else 0.0
        )

        values = {
            "ticker": window.ticker,
            "predicted_date": prediction_date,
            "training_method": training_method,
            "current_state": window.current_state,
            "predicted_state": STATE_LABELS[predicted_index],
            "state_confidence": probs[predicted_index],
            "prob_strong_down": probs[0],
            "prob_down": probs[1],
            "prob_flat": probs[2],
            "prob_up": probs[3],
            "prob_strong_up": probs[4],
            "expected_return": expected_return,
            "entry_price": window.entry_price,
            "exit_price": exit_price,
            "exit_price_confidence": exit_price_confidence,
            "entry_time": ENTRY_TIME,
            "exit_time": EXIT_TIME,
            "history_days": window.history_days,
            "model_version_id": version.id,
            "computed_at": computed_at,
        }
        stmt = sqlite_insert(LstmInference).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[LstmInference.ticker, LstmInference.predicted_date, LstmInference.training_method],
            set_=values,
        )
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info(
        "LSTM (%s)-predicted market state on %s for %d ticker(s) targeting %s using model version %d",
        training_method,
        device,
        stored,
        prediction_date,
        version.id,
    )
    return stored
