"""
Feature Extractor (Phase 3 rebuild)
------------------------------------
Produces continuous engineered features per symbol from daily OHLCV bars.
Deliberately continuous (not thresholded into buy/sell flags) so they can
feed an ML model (Phase 7) rather than a hand-tuned rule engine.

Pure pandas/numpy — no talib/ta package dependency.

Feature families (~25-28 columns per symbol, matches the documented Phase 3 spec):
  - Multi-horizon momentum (5/10/20/60-day returns)
  - Realized volatility at multiple windows (10/20/60-day)
  - ATR and ATR ratio (normalized true range)
  - Continuous ADX / +DI / -DI regime features
  - Bollinger Band %B and z-score (mean-reversion distance)
  - Relative volume (vs rolling average)
  - Cross-sectional relative strength / correlation vs a reference symbol (SPY)

Columns needing more history than is available (e.g. ma_50_200_spread_pct
needing 200 bars) are explicitly omitted with a warning rather than being
silently included as all-NaN dead columns, per the Phase 3 validation fix.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = _true_range(df)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def _adx_di(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = _true_range(df)
    atr_ = tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    plus_dm_s = pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / window, adjust=False, min_periods=window
    ).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / window, adjust=False, min_periods=window
    ).mean()

    plus_di = 100 * (plus_dm_s / atr_.replace(0, np.nan))
    minus_di = 100 * (minus_dm_s / atr_.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx})


def extract_features(
    df: pd.DataFrame,
    reference: pd.DataFrame | None = None,
    symbol: str = "",
) -> pd.DataFrame:
    """
    df: DataFrame indexed by date with columns [open, high, low, close, volume]
    reference: optional reference symbol's df (e.g. SPY) for relative-strength /
               correlation features, same date index.
    """
    df = df.sort_index().copy()
    n = len(df)
    feats = pd.DataFrame(index=df.index)

    close = df["close"]
    volume = df["volume"]

    # --- Multi-horizon momentum ---
    for h in (5, 10, 20, 60):
        feats[f"mom_{h}d"] = close.pct_change(h)

    # --- Realized volatility (annualized, log returns) ---
    log_ret = np.log(close / close.shift(1))
    for w in (10, 20, 60):
        feats[f"realized_vol_{w}d"] = log_ret.rolling(w).std() * np.sqrt(252)

    # --- ATR / ATR ratio (normalized true range) ---
    atr14 = _atr(df, 14)
    feats["atr_14"] = atr14
    feats["atr_ratio"] = atr14 / close  # normalized so it's comparable across price levels

    # --- ADX / DI regime features (continuous, not thresholded) ---
    adx_df = _adx_di(df, 14)
    feats["adx_14"] = adx_df["adx"]
    feats["plus_di_14"] = adx_df["plus_di"]
    feats["minus_di_14"] = adx_df["minus_di"]
    feats["di_spread"] = adx_df["plus_di"] - adx_df["minus_di"]

    # --- Bollinger Bands: %B and z-score mean-reversion distance ---
    for w in (20,):
        ma = close.rolling(w).mean()
        sd = close.rolling(w).std()
        upper = ma + 2 * sd
        lower = ma - 2 * sd
        feats[f"bb_pctb_{w}"] = (close - lower) / (upper - lower)
        feats[f"bb_zscore_{w}"] = (close - ma) / sd.replace(0, np.nan)

    # --- Relative volume ---
    for w in (10, 20):
        feats[f"rel_volume_{w}d"] = volume / volume.rolling(w).mean()

    # --- Moving-average spreads that need enough history ---
    ma_20 = close.rolling(20).mean()
    ma_50 = close.rolling(50).mean()
    feats["ma_20_50_spread_pct"] = (ma_20 - ma_50) / ma_50

    if n >= 200:
        ma_200 = close.rolling(200).mean()
        feats["ma_50_200_spread_pct"] = (ma_50 - ma_200) / ma_200
    else:
        print(
            f"[feature_extractor] {symbol}: only {n} bars available (<200) — "
            f"omitting ma_50_200_spread_pct instead of emitting an all-NaN column"
        )

    # --- Cross-sectional relative strength / correlation vs reference (e.g. SPY) ---
    if reference is not None:
        ref_close = reference["close"].reindex(df.index).ffill()
        ref_ret = ref_close.pct_change()
        sym_ret = close.pct_change()
        feats["rel_strength_20d"] = close.pct_change(20) - ref_close.pct_change(20)
        feats["corr_to_ref_20d"] = sym_ret.rolling(20).corr(ref_ret)
        # simple rolling beta vs reference
        cov = sym_ret.rolling(60).cov(ref_ret)
        var = ref_ret.rolling(60).var()
        feats["beta_to_ref_60d"] = cov / var.replace(0, np.nan)

    return feats


def build_labels(df: pd.DataFrame, horizon: int = 5, threshold: float = 0.0) -> pd.Series:
    """
    Forward-looking label: 1 if forward `horizon`-day return > threshold, else 0.
    NOT leaked into features — uses close.shift(-horizon).
    """
    fwd_ret = df["close"].shift(-horizon) / df["close"] - 1
    return (fwd_ret > threshold).astype(int), fwd_ret
