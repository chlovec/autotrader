"""Trains jobs/lstm_common.py's pooled LSTM the more rigorous way: splits the requested
training window into `num_folds` evenly-spaced rolling cutoffs and, for each one,
retrains a fresh model from scratch on data through that cutoff and evaluates it on the
block of days up to the next cutoff - the same expanding-window, no-lookahead principle
jobs/backtest_market_state.py applies to the Markov chain, coarsened to per-fold blocks
rather than literally every day, since retraining a network daily would be
prohibitively slow.

Only the *final* fold's trained weights (fit on the most data) are saved as this run's
usable jobs/lstm_common.LstmModelVersion checkpoint; every fold's own metrics are
returned as a list of summary lines for jobs/engine.py to fold into that run's
JobRun.result_summary, rather than a new per-fold DB table.

Exists as its own job (rather than a mode flag on jobs/train_lstm_holdout.py)
specifically so this run's total wall-clock time can be compared directly against that
job's on the dashboard's Jobs page, before committing to running this - much more
expensive - flavor regularly.

Purely local aggregation over already-synced bars - no massive.com call involved, same
reasoning as jobs/predict_market_state.py.
"""

import datetime as dt
import logging
import math
import os
import time

import torch

from db.models import LstmModelVersion
from jobs.control import JobControl, report_job_progress
from jobs.lstm_common import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EPOCHS,
    DEFAULT_LEARNING_RATE,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_TRAIN_WINDOW_DAYS,
    DEFAULT_WALKFORWARD_NUM_FOLDS,
    LstmModel,
    build_training_sequences,
    evaluate,
    save_checkpoint,
    select_device,
    split_by_date,
    train_epoch,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("backend_v2.jobs.train_lstm_walkforward")

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "lstm_models")


def _fold_cutoffs(start_date: dt.date, end_date: dt.date, num_folds: int) -> list[dt.date]:
    """num_folds + 1 evenly-spaced boundary dates over [start_date, end_date]
    (inclusive) - cutoffs[k] is fold k's train/evaluate boundary (exclusive on the
    train side, inclusive on the evaluate side), cutoffs[k + 1] - 1 day is fold k's
    last evaluated day. cutoffs[-1] is always exactly end_date + 1 day (an exclusive
    upper bound), so the last fold's evaluation block always reaches all the way to
    end_date regardless of rounding."""
    total_days = (end_date - start_date).days + 1
    cutoffs = [start_date + dt.timedelta(days=round(total_days * i / (num_folds + 1))) for i in range(1, num_folds + 2)]
    cutoffs[-1] = end_date + dt.timedelta(days=1)
    return cutoffs


def train_lstm_walkforward(
    session: Session,
    job_run_id: int,
    train_start_date: dt.date | None = None,
    train_end_date: dt.date | None = None,
    epochs: int = DEFAULT_EPOCHS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    num_folds: int = DEFAULT_WALKFORWARD_NUM_FOLDS,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    control: JobControl | None = None,
) -> tuple[LstmModelVersion, list[str]]:
    """Same date-resolution defaults as jobs/train_lstm_holdout.py's train_lstm_holdout.

    Runs off the event loop (see jobs/engine.py's run_job) - control.checkpoint_sync is
    called once per fold and once per epoch within that fold (previously just once per
    fold), same "an epoch now runs its own validation pass, so it's already a natural,
    individually-slow-enough checkpoint" reasoning as jobs/train_lstm_holdout.py.

    For each fold, one labeled batch is built over [train_start_date, that fold's last
    evaluated day] (jobs/lstm_common.build_training_sequences), then split
    chronologically at that fold's cutoff - same "fit cut points once, split by date"
    reasoning as jobs/train_lstm_holdout.py's single split, just repeated at
    num_folds different boundaries. A fresh LstmModel is trained from scratch each
    fold - no warm-starting from the previous fold's weights - on
    jobs/lstm_common.select_device's best available device.

    Within each fold, evaluates after *every* epoch and keeps that fold's lowest-
    val_loss epoch's weights - same best-epoch selection reasoning as
    jobs/train_lstm_holdout.py's own train_lstm_holdout, applied per fold rather than
    once overall. Only the *final* fold's best epoch is saved as this run's checkpoint
    (see this module's docstring), so a fold's own fold_lines entry reports its best
    epoch's metrics, not its last epoch's.

    Reports combined progress across every fold's epochs (fold_index * epochs +
    epoch_within_fold, out of num_folds * epochs total) via
    jobs/control.report_job_progress(force=True) - same "epochs are too few for the
    normal per-ticker-fan-out throttle to ever show anything but the last update"
    reasoning as jobs/train_lstm_holdout.py.

    Returns the inserted LstmModelVersion row for the *final* fold (already committed,
    with model_path set) plus one human-readable summary line per fold, in order."""
    if train_end_date is None:
        train_end_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    if train_start_date is None:
        train_start_date = train_end_date - dt.timedelta(days=DEFAULT_TRAIN_WINDOW_DAYS)
    if train_start_date >= train_end_date:
        raise ValueError("train_start_date must be before train_end_date")
    if num_folds < 1:
        raise ValueError("num_folds must be at least 1")

    trained_at = dt.datetime.utcnow()
    device = select_device()
    cutoffs = _fold_cutoffs(train_start_date, train_end_date, num_folds)
    total_epochs = num_folds * epochs

    fold_lines: list[str] = []
    final_state = None
    final_train_loss = 0.0
    final_val_loss = 0.0
    final_val_accuracy = 0.0
    total_duration_seconds = 0.0

    report_job_progress(session, job_run_id, 0, total_epochs, force=True)

    for fold_index in range(num_folds):
        if control is not None:
            control.checkpoint_sync()
        fold_started = time.monotonic()

        fold_split_date = cutoffs[fold_index]
        fold_eval_end = cutoffs[fold_index + 1] - dt.timedelta(days=1)
        batch = build_training_sequences(
            session,
            train_start_date,
            fold_eval_end,
            lookback_days=lookback_days,
            ticker_types=ticker_types,
            tickers=tickers,
        )
        train_batch, eval_batch = split_by_date(batch, fold_split_date)
        if len(train_batch) == 0:
            raise ValueError(f"fold {fold_index + 1}/{num_folds} produced no training sequences")

        model = LstmModel().to(device)
        optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

        best_epoch = 0
        best_train_loss = 0.0
        best_val_loss = math.inf
        best_val_accuracy = 0.0
        best_state = None
        for epoch in range(1, epochs + 1):
            if control is not None:
                control.checkpoint_sync()
            epoch_train_loss = train_epoch(model, train_batch, optimizer, batch_size, device)
            epoch_val_loss, epoch_val_accuracy = evaluate(model, eval_batch, batch_size, device)
            if epoch_val_loss < best_val_loss:
                best_epoch = epoch
                best_train_loss = epoch_train_loss
                best_val_loss = epoch_val_loss
                best_val_accuracy = epoch_val_accuracy
                best_state = {name: tensor.detach().cpu().clone() for name, tensor in model.state_dict().items()}
            report_job_progress(session, job_run_id, fold_index * epochs + epoch, total_epochs, force=True)

        assert best_state is not None  # epochs >= 1 is enforced by JobConfig/registry defaults
        fold_duration = time.monotonic() - fold_started
        total_duration_seconds += fold_duration

        fold_lines.append(
            f"fold {fold_index + 1}/{num_folds} [{fold_split_date}..{fold_eval_end}]: "
            f"{len(train_batch)} train / {len(eval_batch)} eval seq(s), best epoch {best_epoch}/{epochs} "
            f"train_loss={best_train_loss:.4f} val_loss={best_val_loss:.4f} val_accuracy={best_val_accuracy:.4f} "
            f"({fold_duration:.1f}s)"
        )
        logger.info("LSTM walk-forward %s", fold_lines[-1])

        final_state = best_state
        final_train_loss, final_val_loss, final_val_accuracy = best_train_loss, best_val_loss, best_val_accuracy

    assert final_state is not None
    version = LstmModelVersion(
        training_method="walkforward",
        job_run_id=job_run_id,
        trained_at=trained_at,
        train_start_date=train_start_date,
        train_end_date=train_end_date,
        lookback_days=lookback_days,
        epochs=epochs,
        learning_rate=learning_rate,
        batch_size=batch_size,
        num_folds=num_folds,
        ticker_types=",".join(ticker_types) if ticker_types else None,
        tickers=",".join(tickers) if tickers else None,
        train_loss=final_train_loss,
        val_loss=final_val_loss,
        val_accuracy=final_val_accuracy,
        duration_seconds=total_duration_seconds,
        model_path="",
    )
    session.add(version)
    session.commit()

    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, f"{version.id}.pt")
    final_model = LstmModel()
    final_model.load_state_dict(final_state)
    save_checkpoint(final_model, model_path)
    version.model_path = model_path
    session.commit()

    logger.info(
        "trained LSTM (walk-forward, %d fold(s)) on %s over [%s..%s]: final fold's best val_accuracy=%.4f "
        "in %.1fs total",
        num_folds,
        device,
        train_start_date,
        train_end_date,
        final_val_accuracy,
        total_duration_seconds,
    )
    return version, fold_lines
