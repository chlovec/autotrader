# Autotrader

Autotrader is a market research app that allows users to predict market states as well as pick stocks to trade. Users can choose stocks based on their trading pattern, which can be day trading, short-term or long term trading.

## Jobs

There are a number of jobs to run, they can be run manually or scheduled to run automatically.

1. Predict Market State:
Predict market state is a job that utilizes classical statistics to predict the market state per ticker. It generates two predictions per ticker - Markov Expected Return and MCMC Expected Return. It uses a two-phase job to make the predictions. None of the two job does not require any separate training step or a saved model.

1.1 The Markov Chain job fits a first-order markov chain per ticker from the tickers discretized daily run history and returns a deterministic (argmax) prediction.

1.2 The Monte Carlo (MCMC) Simulation job runs thousands of random walks over the same fitted chain to produce a full exit price distribution (mean/std/percentile bands).


Three different modeling approaches, all predicting the same thing (next-session state: strong_down/down/flat/up/strong_up, plus expected return/entry-exit price) so they're directly comparable on the Prediction Comparison report:

**`predict-market-state`** ([jobs/predict_market_state.py](backend-v2/jobs/predict_market_state.py), [jobs/predict_market_state_mcmc.py](backend-v2/jobs/predict_market_state_mcmc.py))
- Classical/statistical, not a neural net. Fits a first-order **Markov chain** per ticker, fresh on every run, directly from that ticker's discretized daily-return history — no separate training step or saved model.
- One job actually runs two phases back-to-back: the Markov chain gives one deterministic (argmax) prediction, then a Monte Carlo simulation runs thousands of random walks over that same fitted chain to produce a full exit-price *distribution* (mean/std/percentile bands), not just a point estimate.
- Writes to `market_predictions` + `market_predictions_mcmc`.

**`predict-market-state (LSTM, holdout)`** and **`predict-market-state (LSTM, walk-forward)`** ([jobs/predict_lstm_market_state.py](backend-v2/jobs/predict_lstm_market_state.py))
- A trained **LSTM neural network** ([jobs/lstm_common.py](backend-v2/jobs/lstm_common.py)), pooled across tickers rather than fit per-ticker. These two jobs only run *inference* — they load a checkpoint that a separate training job already produced and write its output to `lstm_inferences`.
- The "holdout" vs. "walk-forward" split is about **how the model was trained**, not the inference job itself:
  - `train-lstm-holdout` ([jobs/train_lstm_holdout.py](backend-v2/jobs/train_lstm_holdout.py)): one chronological train/validation split — trains on the first ~85% of the window by date, validates once on the last ~15%. Fast, one pass.
  - `train-lstm-walkforward` ([jobs/train_lstm_walkforward.py](backend-v2/jobs/train_lstm_walkforward.py)): several rolling, expanding-window folds — retrains from scratch at each cutoff and evaluates on the next block, same no-lookahead principle as the Markov backtest job but coarsened to folds. More rigorous, much more expensive; only the final fold's weights become the usable checkpoint.
- `predict-market-state-lstm-holdout` always runs the latest `train-lstm-holdout`-flavor checkpoint; `predict-market-state-lstm-walkforward` always runs the latest `train-lstm-walkforward`-flavor one. They're kept as fully independent jobs (own schedule, own run history, own `lstm_inferences` rows tagged by `training_method`) specifically so you can compare the two training strategies' live prediction quality side by side, rather than one silently overwriting the other.

So: one is a lightweight statistical model that needs no training, and the other two are the same neural-net architecture and inference code, differing only in which of two training regimes (quick single-split vs. expensive multi-fold walk-forward) produced the checkpoint they're running.