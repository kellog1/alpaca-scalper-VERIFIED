"""
Single definition of the volatility-expansion target.

Both the trainer (ml_ensemble_vol.build_vol_label) and the live scorer
(trade_analytics.vol_calibration_report) import this, so the model is trained
and judged against the same window. Pure pandas/numpy (no xgboost) so it can
be tested anywhere.

For a row at bar t (the last bar known when the prediction was made):
    trailing vol = std of log returns over bars t-baseline_window+1 .. t
    forward  vol = std of log returns over bars t+1 .. t+horizon
    ratio        = forward / trailing
    expansion    = ratio > EXPANSION_THRESHOLD

Rows whose forward window has not fully elapsed (the last `horizon` rows) or
whose trailing window is incomplete/zero have ratio NaN and label NaN. They
are unknown, not "no expansion", and must be dropped, never trained as 0.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

HORIZON = 10
BASELINE_WINDOW = 20
EXPANSION_THRESHOLD = 1.10   # >10% relative expansion counts as an expansion


def vol_ratio(close: pd.Series, horizon: int = HORIZON, baseline_window: int = BASELINE_WINDOW) -> pd.Series:
    log_ret = np.log(close / close.shift(1))
    trailing = log_ret.rolling(baseline_window).std()
    # rolling(h) at bar t+h covers returns t+1..t+h; shifting back by h aligns it to bar t.
    forward = log_ret.rolling(horizon).std().shift(-horizon)
    return forward / trailing.replace(0, np.nan)


def expansion_label(ratio: pd.Series, threshold: float = EXPANSION_THRESHOLD) -> pd.Series:
    """1.0 / 0.0 where the ratio is known, NaN where it is not."""
    return (ratio > threshold).astype(float).where(ratio.notna())
