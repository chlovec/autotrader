"""Shared LSTM model, feature engineering, and train/eval loop used by both
jobs/train_lstm_holdout.py and jobs/train_lstm_walkforward.py (the two validation
flavors), and loaded by jobs/predict_lstm_market_state.py for inference. Kept in one
module so the model architecture and loss function can never drift between the two
training jobs - only the date-windowing/validation strategy around them differs.

Pooled (cross-ticker), unlike jobs/predict_market_state.py's per-ticker Markov chain: a
single ticker's few hundred daily bars is nowhere near enough data to train a network,
so this trains one global model across every selected ticker's sliding windows at once.
Predicts the same STATE_LABELS quantile bucket predict_market_state.py fits (so all
three models' "predicted_state" mean the same thing), plus a regression head for the
raw next-period return - see jobs/predict_market_state.STATE_LABELS/_cut_points/
_bucket_one, reused directly rather than refit here.

Features are engineered fresh from ohlc_bars, per bar in a ticker's lookback window:
close-to-close return (computed the same way predict_market_state.py computes it - the
value STATE_LABELS buckets are actually fit on), OhlcBar.pcnt_increase (the *intraday*
open-to-close move - a distinct signal from the close-to-close return above, already
precomputed), (high - low) / close (intraday range), and log-volume z-scored *within
each window* (so a low- vs high-float ticker's volume is comparable without needing a
cross-ticker normalization table). Deliberately not joining technical_indicators/
average_volumes - their coverage is far sparser than ohlc_bars itself (roughly 4-5k of
~30k tickers per this project's earlier survey), which would silently exclude most of
the ticker universe from training.
"""

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from itertools import groupby

import numpy as np
import torch
from sqlalchemy import select
from sqlalchemy.orm import Session
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from db.models import OhlcBar
from jobs.predict_market_state import (
    DEFAULT_MULTIPLIER,
    DEFAULT_TIMESPAN,
    STATE_LABELS,
    _apply_ticker_filter,
    _bucket_states,
    _cut_points,
)

NUM_STATES = len(STATE_LABELS)
NUM_FEATURES = 4  # close-to-close return, pcnt_increase, high-low range, log-volume z-score

# Overridable via JobConfig.lstm_* fields (see db/models.py's JobConfig docstring) -
# resolved here, at run time, same "left None, resolved by the job module" convention
# every other optional JobConfig field already follows.
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_EPOCHS = 5
DEFAULT_LEARNING_RATE = 1e-3
DEFAULT_BATCH_SIZE = 256
DEFAULT_WALKFORWARD_NUM_FOLDS = 3
# lstm_train_start_date's fallback distance before lstm_train_end_date when unset -
# matches jobs/sync_ohlc_bars.py's own 2-year default and this DB's actual bar
# coverage (see this project's earlier survey: ohlc_bars spans ~2 years).
DEFAULT_TRAIN_WINDOW_DAYS = 730

# Weight of the regression loss term relative to the classification term in
# train_epoch/evaluate's combined loss - a fixed module constant, not user-configurable,
# same "reasonable default, not every knob exposed" reasoning as
# jobs/predict_market_state_mcmc.py's COMMIT_BATCH_SIZE.
REGRESSION_LOSS_WEIGHT = 0.5

# Fixed architecture constants - not JobConfig fields, since load_checkpoint needs to
# reconstruct the exact same architecture a saved state_dict was trained under, and
# letting these vary per run would mean also persisting them per LstmModelVersion just
# to load a checkpoint back. If this ever needs to be configurable, both concerns have
# to be solved together.
HIDDEN_SIZE = 32
DROPOUT = 0.2

# train_epoch/evaluate's default `device` - a module-level singleton rather than a
# `torch.device("cpu")` call in the signature itself, since a fresh torch.device call
# per default-argument evaluation (which happens once, at import time, either way) is
# a lint smell some tooling flags regardless of it being harmless here.
_CPU_DEVICE = torch.device("cpu")


def select_device() -> torch.device:
    """CUDA if available, else Apple Silicon MPS, else CPU - checked in that order
    every time (not cached) since it's cheap and this repo's own dev machine only ever
    has MPS, never CUDA, so exercising both branches matters for portability to a
    CUDA box. Used by both training jobs' epoch loop and jobs/predict_lstm_market_state.py's
    batched inference pass - benchmarked ~2.5-4.5x faster than CPU for this model on
    this machine's MPS backend in both directions, even accounting for the CPU<->device
    tensor transfer a DataLoader-fed loop or a single big inference batch each pay."""
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class LstmModel(nn.Module):
    """Single LSTM layer + dropout + two linear heads off the final hidden state - a
    NUM_STATES-way classification head (softmax applied by the caller, not baked in,
    so training can use cross_entropy's built-in log-softmax) and a 1-unit regression
    head for the next-period return."""

    def __init__(self, hidden_size: int = HIDDEN_SIZE, dropout: float = DROPOUT) -> None:
        super().__init__()
        self.lstm = nn.LSTM(input_size=NUM_FEATURES, hidden_size=hidden_size, batch_first=True)
        self.dropout = nn.Dropout(dropout)
        self.classifier = nn.Linear(hidden_size, NUM_STATES)
        self.regressor = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        _, (hidden, _) = self.lstm(x)
        last = self.dropout(hidden[-1])
        return self.classifier(last), self.regressor(last).squeeze(-1)


@dataclass
class SequenceBatch:
    """Output of build_training_sequences - one entry per (ticker, target_date)
    sliding window whose target_date falls within the requested range. target_dates is
    kept alongside the tensors (not folded into them) so a caller can carve a single
    fetched-and-labeled batch into a chronological train/validation split (see
    split_by_date below) without re-fitting each side's quantile cut points on a
    different, inconsistent window of history."""

    features: torch.Tensor  # (N, lookback_days, NUM_FEATURES), float32
    state_targets: torch.Tensor  # (N,), int64
    return_targets: torch.Tensor  # (N,), float32
    target_dates: list[dt.date]

    def __len__(self) -> int:
        return self.features.shape[0]


def split_by_date(batch: SequenceBatch, split_date: dt.date) -> tuple[SequenceBatch, SequenceBatch]:
    """Splits `batch` into (before split_date, on-or-after split_date) - purely a
    row-selection split, no re-fitting - used by jobs/train_lstm_holdout.py's single
    chronological holdout and jobs/train_lstm_walkforward.py's per-fold evaluation
    windows alike."""
    train_idx = [i for i, d in enumerate(batch.target_dates) if d < split_date]
    val_idx = [i for i, d in enumerate(batch.target_dates) if d >= split_date]

    def _subset(indices: list[int]) -> SequenceBatch:
        idx_tensor = torch.tensor(indices, dtype=torch.int64)
        return SequenceBatch(
            features=batch.features[idx_tensor] if indices else batch.features[:0],
            state_targets=batch.state_targets[idx_tensor] if indices else batch.state_targets[:0],
            return_targets=batch.return_targets[idx_tensor] if indices else batch.return_targets[:0],
            target_dates=[batch.target_dates[i] for i in indices],
        )

    return _subset(train_idx), _subset(val_idx)


@dataclass
class InferenceWindow:
    """One ticker's most recent lookback_days window as of a prediction cutoff - see
    build_inference_windows."""

    ticker: str
    features: torch.Tensor  # (lookback_days, NUM_FEATURES), float32
    entry_price: float
    current_state: str
    history_days: int
    # Historical stdev of return, one per STATE_LABELS bucket, fit over this ticker's
    # own return history the same way jobs/predict_market_state.py's own
    # compute_market_state_predictions fits bucket_stds - lets
    # jobs/predict_lstm_market_state.py derive exit_price_confidence from the predicted
    # state's own historical spread, same formula shape as that job's, rather than the
    # LSTM's classification confidence (state_confidence measures confidence in *which*
    # state was predicted, not how tight that state's own return distribution is).
    bucket_stds: list[float]


def _fetch_ticker_bars(
    session: Session,
    ticker_types: list[str] | None,
    tickers: list[str] | None,
    end_date: dt.date,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
):
    """Every matching ticker's full bar history through end_date (inclusive), grouped -
    same query shape as jobs/predict_market_state.py's own fetch, extended with the
    high/low/volume/pcnt_increase columns feature-building needs. Bars after end_date
    are never fetched, so a training/inference run can never see data that wouldn't
    actually have been available yet as of that date."""
    cutoff = dt.datetime.combine(end_date, dt.time.max)
    query = (
        select(
            OhlcBar.ticker,
            OhlcBar.timestamp,
            OhlcBar.close,
            OhlcBar.high,
            OhlcBar.low,
            OhlcBar.volume,
            OhlcBar.pcnt_increase,
        )
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # Same bad-$0-print guard as jobs/predict_market_state.py.
            OhlcBar.close > 0,
            OhlcBar.timestamp <= cutoff,
        )
        .order_by(OhlcBar.ticker, OhlcBar.timestamp)
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    rows = session.execute(query).all()
    return groupby(rows, key=lambda row: row.ticker)


def _window_features(
    closes: list[float],
    highs: list[float | None],
    lows: list[float | None],
    volumes: list[float | None],
    pcnts: list[float | None],
    end_index: int,
    lookback_days: int,
) -> list[list[float]]:
    """The lookback_days-long feature window ending at bar index end_index (inclusive).
    Requires end_index - lookback_days + 1 >= 1 (each step needs a prior close to
    compute its own close-to-close return from) - callers are responsible for only
    calling this where that holds."""
    start_index = end_index - lookback_days + 1
    raw_rows: list[tuple[float, float, float, float]] = []
    for idx in range(start_index, end_index + 1):
        ret = closes[idx] / closes[idx - 1] - 1
        # pcnt_increase is stored as a percent (e.g. 1.5, not 0.015) - divided down to
        # the same fractional scale as ret/rng so no single feature dominates the
        # network purely from unit scale.
        pcnt = (pcnts[idx] / 100.0) if pcnts[idx] is not None else 0.0
        high, low, close = highs[idx], lows[idx], closes[idx]
        rng = (high - low) / close if high is not None and low is not None and close else 0.0
        volume = volumes[idx] if volumes[idx] is not None else 0.0
        raw_rows.append((ret, pcnt, rng, volume))

    log_volumes = [math.log1p(max(volume, 0.0)) for *_, volume in raw_rows]
    mean_lv = statistics.mean(log_volumes)
    std_lv = statistics.stdev(log_volumes) if len(log_volumes) >= 2 else 0.0
    return [
        [ret, pcnt, rng, (lv - mean_lv) / std_lv if std_lv > 0 else 0.0]
        for (ret, pcnt, rng, _), lv in zip(raw_rows, log_volumes)
    ]


def _ticker_training_sequences(
    closes: list[float],
    highs: list[float | None],
    lows: list[float | None],
    volumes: list[float | None],
    pcnts: list[float | None],
    dates: list[dt.date],
    lookback_days: int,
    start_date: dt.date,
    end_date: dt.date,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dt.date]]:
    """Vectorized equivalent of calling _window_features once per k in
    range(lookback_days, len(closes) - 1) and bucketing each result with _bucket_one -
    same per-bar features, same "cut points fit once over this ticker's entire return
    history" contract, same within-window (not global) log-volume z-score - just built
    with numpy over the whole ticker at once instead of a Python loop per window per
    bar. Necessary because build_training_sequences' true target universe (every
    (ticker, day) pair, not a single ticker) makes the pure-Python nested loop the
    dominant cost of a training run - unlike _window_features' single-window call from
    build_inference_windows, which stays on the simple per-window path since there's
    only ever one window to build there.

    Returns arrays already restricted to target_date in [start_date, end_date], ready
    to be concatenated across tickers by the caller."""
    closes_arr = np.asarray(closes, dtype=np.float64)
    highs_arr = np.array([h if h is not None else np.nan for h in highs], dtype=np.float64)
    lows_arr = np.array([low if low is not None else np.nan for low in lows], dtype=np.float64)
    volumes_arr = np.array([v if v is not None else np.nan for v in volumes], dtype=np.float64)
    pcnts_arr = np.array([p if p is not None else np.nan for p in pcnts], dtype=np.float64)

    # returns_arr[idx] is the return realized moving from closes[idx - 1] to
    # closes[idx] - i.e. the return "on" dates[idx]. Index 0 is left undefined (never
    # read): the first window's end index is lookback_days >= 2, so target/feature
    # indices into this array always start at 1. Same indexing convention
    # jobs/backtest_market_state.py's all_returns uses, shifted by one to be directly
    # indexable by bar index rather than by "i-th return".
    returns_arr = np.empty(len(closes_arr), dtype=np.float64)
    returns_arr[1:] = closes_arr[1:] / closes_arr[:-1] - 1

    # Quantile cut points fit over this ticker's *entire* fetched return history - same
    # "fit over the same window used for prediction" convention
    # jobs/predict_market_state.py's own live runs already use.
    cut_points = np.asarray(_cut_points(returns_arr[1:].tolist()))

    pcnt_feat = np.where(np.isnan(pcnts_arr), 0.0, pcnts_arr / 100.0)
    has_range = ~np.isnan(highs_arr) & ~np.isnan(lows_arr)
    rng_feat = np.where(has_range, (highs_arr - lows_arr) / closes_arr, 0.0)
    volume_filled = np.where(np.isnan(volumes_arr), 0.0, volumes_arr)
    # Raw (not yet z-scored) log-volume - z-scoring happens per-window below, since
    # _window_features' own mean/std is restricted to each window's lookback_days
    # values, not the ticker's full history. log1p is applied per-bar regardless of
    # windowing, so computing it once up front and windowing afterward yields the exact
    # same per-bar values _window_features computed inside its own loop.
    log_volume_feat = np.log1p(np.clip(volume_filled, 0.0, None))

    feat = np.stack([returns_arr, pcnt_feat, rng_feat, log_volume_feat], axis=1)  # (num_bars, NUM_FEATURES)

    num_bars = len(closes_arr)
    windows = np.lib.stride_tricks.sliding_window_view(feat, window_shape=lookback_days, axis=0)
    windows = np.transpose(windows, (0, 2, 1))  # (num_bars - lookback_days + 1, lookback_days, NUM_FEATURES)
    # windows[j] spans bar indices [j, j + lookback_days - 1] -> end index k = j +
    # lookback_days - 1. Only k in [lookback_days, num_bars - 2] are valid samples (same
    # bound the original range(lookback_days, len(closes) - 1) enforced), i.e. j in
    # [1, num_bars - lookback_days - 1] - a copy is required since sliding_window_view
    # returns a read-only, overlapping-memory view and the z-score step below writes
    # into it.
    windows = windows[1 : num_bars - lookback_days].copy()

    log_vol_window = windows[:, :, 3]
    mean_lv = log_vol_window.mean(axis=1)
    std_lv = log_vol_window.std(axis=1, ddof=1)  # sample stdev, matching statistics.stdev
    windows[:, :, 3] = np.where(std_lv[:, None] > 0, (log_vol_window - mean_lv[:, None]) / std_lv[:, None], 0.0)

    end_indices = np.arange(1, num_bars - lookback_days) + lookback_days - 1
    target_returns = returns_arr[end_indices + 1]
    target_states = np.searchsorted(cut_points, target_returns, side="right")
    target_dates_all = [dates[i + 1] for i in end_indices]

    date_mask = np.array([start_date <= d <= end_date for d in target_dates_all])
    kept_dates = [d for d, keep in zip(target_dates_all, date_mask, strict=True) if keep]
    return (
        windows[date_mask].astype(np.float32),
        target_states[date_mask].astype(np.int64),
        target_returns[date_mask].astype(np.float32),
        kept_dates,
    )


def build_training_sequences(
    session: Session,
    start_date: dt.date,
    end_date: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
) -> SequenceBatch:
    """One sample per (ticker, target_date) sliding window whose target_date - the date
    of the return the sample is labeled with - falls within [start_date, end_date].

    Per ticker, quantile cut points (jobs/predict_market_state._cut_points) are fit
    once over that ticker's *entire* fetched return history through end_date - the same
    "fit over the same window used for prediction" convention
    jobs/predict_market_state.py's own live runs already use, not the stricter
    fit-through-yesterday-only convention jobs/backtest_market_state.py uses purely for
    backtest rigor. A ticker with fewer than lookback_days + 2 bars (not enough to form
    even one full window plus a target) contributes no samples.

    Per-ticker work is vectorized with numpy (_ticker_training_sequences) rather than
    looped in pure Python - across the full ticker universe this is a ~7M-sample
    operation, and a Python-level loop over every (ticker, window, lookback-day) triple
    was the dominant cost of a training run."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2")

    feature_chunks: list[np.ndarray] = []
    state_chunks: list[np.ndarray] = []
    return_chunks: list[np.ndarray] = []
    all_target_dates: list[dt.date] = []

    for _ticker, group in _fetch_ticker_bars(session, ticker_types, tickers, end_date, multiplier, timespan):
        bars = list(group)
        if len(bars) < lookback_days + 2:
            continue
        features, state_targets, return_targets, target_dates = _ticker_training_sequences(
            closes=[bar.close for bar in bars],
            highs=[bar.high for bar in bars],
            lows=[bar.low for bar in bars],
            volumes=[bar.volume for bar in bars],
            pcnts=[bar.pcnt_increase for bar in bars],
            dates=[bar.timestamp.date() for bar in bars],
            lookback_days=lookback_days,
            start_date=start_date,
            end_date=end_date,
        )
        if len(target_dates) == 0:
            continue
        feature_chunks.append(features)
        state_chunks.append(state_targets)
        return_chunks.append(return_targets)
        all_target_dates.extend(target_dates)

    if feature_chunks:
        features_arr = np.concatenate(feature_chunks, axis=0)
        state_arr = np.concatenate(state_chunks, axis=0)
        return_arr = np.concatenate(return_chunks, axis=0)
    else:
        features_arr = np.empty((0, lookback_days, NUM_FEATURES), dtype=np.float32)
        state_arr = np.empty(0, dtype=np.int64)
        return_arr = np.empty(0, dtype=np.float32)

    return SequenceBatch(
        features=torch.from_numpy(features_arr),
        state_targets=torch.from_numpy(state_arr),
        return_targets=torch.from_numpy(return_arr),
        target_dates=all_target_dates,
    )


def build_inference_windows(
    session: Session,
    prediction_date: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
) -> list[InferenceWindow]:
    """One InferenceWindow per qualifying ticker - its most recent lookback_days window
    using only bars strictly before prediction_date, same causal-cutoff convention
    jobs/predict_market_state_mcmc.py's own query uses. A ticker with fewer than
    lookback_days + 1 bars (not enough to form one full window) is skipped."""
    end_date = prediction_date - dt.timedelta(days=1)
    windows: list[InferenceWindow] = []
    for ticker, group in _fetch_ticker_bars(session, ticker_types, tickers, end_date, multiplier, timespan):
        bars = list(group)
        closes = [bar.close for bar in bars]
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        volumes = [bar.volume for bar in bars]
        pcnts = [bar.pcnt_increase for bar in bars]
        if len(closes) < lookback_days + 1:
            continue

        returns = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)]
        buckets, _bucket_means, bucket_stds = _bucket_states(returns)
        current_state = STATE_LABELS[buckets[-1]]
        last_index = len(closes) - 1
        features = _window_features(closes, highs, lows, volumes, pcnts, last_index, lookback_days)
        windows.append(
            InferenceWindow(
                ticker=ticker,
                features=torch.tensor(features, dtype=torch.float32),
                entry_price=closes[-1],
                current_state=current_state,
                history_days=len(returns),
                bucket_stds=bucket_stds,
            )
        )
    return windows


def _combined_loss(
    class_logits: torch.Tensor,
    regression_pred: torch.Tensor,
    state_targets: torch.Tensor,
    return_targets: torch.Tensor,
) -> torch.Tensor:
    classification_loss = nn.functional.cross_entropy(class_logits, state_targets)
    regression_loss = nn.functional.mse_loss(regression_pred, return_targets)
    return classification_loss + REGRESSION_LOSS_WEIGHT * regression_loss


def _make_loader(batch: SequenceBatch, batch_size: int, shuffle: bool) -> DataLoader:
    dataset = TensorDataset(batch.features, batch.state_targets, batch.return_targets)
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)


def train_epoch(
    model: LstmModel,
    batch: SequenceBatch,
    optimizer: torch.optim.Optimizer,
    batch_size: int,
    device: torch.device = _CPU_DEVICE,
) -> float:
    """One pass over `batch`, shuffled into mini-batches - shuffling mini-batch order
    doesn't violate causality, since each individual sample's own features only ever
    come from dates strictly before its own target_date regardless of the order
    batches are drawn in. Returns the mean training loss across mini-batches.

    `batch` itself stays on the CPU (it's built by build_training_sequences, which
    never touches a device) - `model` is assumed already moved to `device` by the
    caller (see jobs/train_lstm_holdout.py/train_lstm_walkforward.py, both via
    select_device), and each mini-batch is moved just before its forward pass, same
    "transfer per batch, not the whole dataset at once" tradeoff the benchmark backing
    select_device's docstring measured."""
    model.train()
    total_loss = 0.0
    num_batches = 0
    for features, state_targets, return_targets in _make_loader(batch, batch_size, shuffle=True):
        features = features.to(device)
        state_targets = state_targets.to(device)
        return_targets = return_targets.to(device)
        optimizer.zero_grad()
        class_logits, regression_pred = model(features)
        loss = _combined_loss(class_logits, regression_pred, state_targets, return_targets)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()
        num_batches += 1
    return total_loss / num_batches if num_batches else 0.0


@torch.no_grad()
def evaluate(
    model: LstmModel, batch: SequenceBatch, batch_size: int, device: torch.device = _CPU_DEVICE
) -> tuple[float, float]:
    """Mean loss and classification accuracy over `batch`, no gradient/shuffling -
    used against a held-out validation batch by both training jobs, once per epoch (see
    those jobs' own docstrings for why - best-epoch model selection needs a validation
    score after every epoch, not just once at the end). Same per-mini-batch device
    transfer as train_epoch."""
    if len(batch) == 0:
        return 0.0, 0.0
    model.eval()
    total_loss = 0.0
    correct = 0
    num_batches = 0
    for features, state_targets, return_targets in _make_loader(batch, batch_size, shuffle=False):
        features = features.to(device)
        state_targets = state_targets.to(device)
        return_targets = return_targets.to(device)
        class_logits, regression_pred = model(features)
        loss = _combined_loss(class_logits, regression_pred, state_targets, return_targets)
        total_loss += loss.item()
        num_batches += 1
        correct += (class_logits.argmax(dim=1) == state_targets).sum().item()
    accuracy = correct / len(batch)
    mean_loss = total_loss / num_batches if num_batches else 0.0
    return mean_loss, accuracy


def save_checkpoint(model: LstmModel, path: str) -> None:
    torch.save(model.state_dict(), path)


def load_checkpoint(path: str) -> LstmModel:
    model = LstmModel()
    model.load_state_dict(torch.load(path, map_location="cpu"))
    model.eval()
    return model
