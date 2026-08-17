"""Trains jobs/lstm_common.py's pooled LSTM with a single chronological validation
holdout: the last ~15% of days (by date, not shuffled) within the requested training
window are held out, the model trains on everything before that, and is evaluated once
on the holdout. Fast - one training pass - at the cost of only testing one train/
validation boundary, unlike jobs/train_lstm_walkforward.py's several rolling folds.

Exists as its own job (rather than a mode flag on one job) specifically so its
JobRun.started_at/finished_at can be compared directly against
jobs/train_lstm_walkforward.py's on the dashboard's Jobs page, before committing to
running the more expensive flavor regularly.

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
    LstmModel,
    build_training_sequences,
    evaluate,
    save_checkpoint,
    select_device,
    split_by_date,
    train_epoch,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("backend_v2.jobs.train_lstm_holdout")

# Fraction of the requested [train_start_date, train_end_date] window (by day count,
# not sample count) held out for validation - the remainder is trained on.
HOLDOUT_FRACTION = 0.15

# Checkpoints are saved under here, one file per LstmModelVersion row - same
# data/-directory convention as jobs/sync_etf_constituents.py's holdings files.
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "lstm_models")


def _holdout_split_date(start_date: dt.date, end_date: dt.date) -> dt.date:
    total_days = (end_date - start_date).days
    holdout_days = max(1, round(total_days * HOLDOUT_FRACTION))
    return end_date - dt.timedelta(days=holdout_days - 1)


def train_lstm_holdout(
    session: Session,
    job_run_id: int,
    train_start_date: dt.date | None = None,
    train_end_date: dt.date | None = None,
    epochs: int = DEFAULT_EPOCHS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    control: JobControl | None = None,
) -> LstmModelVersion:
    """`train_end_date` defaults to yesterday (UTC), `train_start_date` to
    DEFAULT_TRAIN_WINDOW_DAYS calendar days before that - same "resolve a None date at
    run time" reasoning as jobs/average_volume.py's compute_average_volume.

    Runs off the event loop (see jobs/engine.py's run_job) - control.checkpoint_sync is
    called once before sequence-building and once per epoch thereafter (previously just
    once more before a single end-of-run evaluation): each epoch now runs its own
    validation pass (see best-model selection below), so an epoch boundary is already a
    natural, individually-slow-enough checkpoint, unlike a training epoch's own internal
    mini-batches.

    Fits one quantile-bucketed target label per (ticker, day) over the *entire*
    [train_start_date, train_end_date] window (jobs/lstm_common.build_training_sequences),
    then splits that single labeled batch chronologically - the last ~15% of days held
    out for validation - rather than re-fitting cut points separately per side, so the
    train and validation sets' target labels stay defined consistently.

    Trains on jobs/lstm_common.select_device's best available device (CUDA, then Apple
    Silicon MPS, then CPU - see that function's docstring for the measured speedup).

    Evaluates on the holdout split after *every* epoch (not just once at the end) and
    keeps the lowest-val_loss epoch's weights - the training loss keeps falling every
    epoch by construction, but the held-out validation loss can start rising again once
    the model overfits, so "whichever epoch finished last" and "whichever epoch actually
    generalized best" aren't guaranteed to be the same epoch. The returned
    LstmModelVersion's train_loss/val_loss/val_accuracy and the checkpoint saved to disk
    are that best epoch's, not necessarily the final one's - see jobs/predict_lstm_market_state.py,
    which only ever loads this row's saved checkpoint.

    Reports per-epoch progress via jobs/control.report_job_progress(force=True) - epochs
    are few enough (single digits, typically) that the throttle report_job_progress
    normally applies for a tens-of-thousands-strong per-ticker fan-out would otherwise
    hide every update but the last.

    Returns the inserted LstmModelVersion row (already committed, with model_path set)."""
    if train_end_date is None:
        train_end_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    if train_start_date is None:
        train_start_date = train_end_date - dt.timedelta(days=DEFAULT_TRAIN_WINDOW_DAYS)
    if train_start_date >= train_end_date:
        raise ValueError("train_start_date must be before train_end_date")

    started_at = time.monotonic()
    trained_at = dt.datetime.utcnow()
    device = select_device()

    if control is not None:
        control.checkpoint_sync()
    report_job_progress(session, job_run_id, 0, epochs, force=True)

    batch = build_training_sequences(
        session,
        train_start_date,
        train_end_date,
        lookback_days=lookback_days,
        ticker_types=ticker_types,
        tickers=tickers,
    )
    split_date = _holdout_split_date(train_start_date, train_end_date)
    train_batch, val_batch = split_by_date(batch, split_date)
    if len(train_batch) == 0:
        raise ValueError("no training sequences produced for the requested date range/tickers")

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
        epoch_val_loss, epoch_val_accuracy = evaluate(model, val_batch, batch_size, device)
        if epoch_val_loss < best_val_loss:
            best_epoch = epoch
            best_train_loss = epoch_train_loss
            best_val_loss = epoch_val_loss
            best_val_accuracy = epoch_val_accuracy
            best_state = {name: tensor.detach().cpu().clone() for name, tensor in model.state_dict().items()}
        report_job_progress(session, job_run_id, epoch, epochs, force=True)

    assert best_state is not None  # epochs >= 1 is enforced by JobConfig/registry defaults
    duration_seconds = time.monotonic() - started_at

    version = LstmModelVersion(
        training_method="holdout",
        job_run_id=job_run_id,
        trained_at=trained_at,
        train_start_date=train_start_date,
        train_end_date=train_end_date,
        lookback_days=lookback_days,
        epochs=epochs,
        learning_rate=learning_rate,
        batch_size=batch_size,
        num_folds=None,
        ticker_types=",".join(ticker_types) if ticker_types else None,
        tickers=",".join(tickers) if tickers else None,
        train_loss=best_train_loss,
        val_loss=best_val_loss,
        val_accuracy=best_val_accuracy,
        duration_seconds=duration_seconds,
        model_path="",
    )
    session.add(version)
    session.commit()

    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, f"{version.id}.pt")
    model.load_state_dict(best_state)
    save_checkpoint(model, model_path)
    version.model_path = model_path
    session.commit()

    logger.info(
        "trained LSTM (holdout) on %s for %d sequence(s) [%s..%s), validated on %d sequence(s) [%s..%s]: "
        "best epoch %d/%d train_loss=%.4f val_loss=%.4f val_accuracy=%.4f in %.1fs",
        device,
        len(train_batch),
        train_start_date,
        split_date,
        len(val_batch),
        split_date,
        train_end_date,
        best_epoch,
        epochs,
        best_train_loss,
        best_val_loss,
        best_val_accuracy,
        duration_seconds,
    )
    return version
