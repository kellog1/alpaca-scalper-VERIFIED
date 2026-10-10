"""Offline checks for the vol-expansion label and its live scorer: python test_vol_label.py"""
import numpy as np
import pandas as pd
from vol_label import vol_ratio, expansion_label, HORIZON, BASELINE_WINDOW
from ml_ensemble_vol import build_vol_label
from trade_analytics import vol_calibration_report

# Deterministic series: alternating +/-0.1% returns (ratio ~1.03, never an expansion)
# with ONE 5% shock return at bar SHOCK.
N, SHOCK = 120, 60
r = np.array([0.001 if i % 2 else -0.001 for i in range(N)])
r[0] = 0.0
r[SHOCK] = 0.05
close = pd.Series(100 * np.exp(np.cumsum(r)))
df = pd.DataFrame({"close": close})
log_ret = np.log(close / close.shift(1))

# --- window alignment: label at t looks at returns t+1..t+10, trailing is t-19..t ---
ratio = vol_ratio(close)
for t in (30, 49, 50, 59, 60, 80):
    want = log_ret.iloc[t + 1:t + 1 + HORIZON].std() / log_ret.iloc[t - BASELINE_WINDOW + 1:t + 1].std()
    assert np.isclose(ratio.iloc[t], want), (t, ratio.iloc[t], want)

# Behavioural check: the shock at bar 60 is inside the forward window only for t = 50..59.
label, _ = build_vol_label(df)
assert label.iloc[49] == 0, "shock is 11 bars out: not in t+1..t+10 (old t+10..t+19 window said 1)"
assert all(label.iloc[t] == 1 for t in range(50, 60)), list(label.iloc[48:62])
assert label.iloc[60] == 0     # shock is now behind us and inflates the trailing vol

# --- unknown is NaN, not 0 ---
assert label.iloc[-HORIZON:].isna().all(), "forward window not elapsed: must be unknown"
assert label.iloc[-HORIZON - 1] == 0 and not np.isnan(label.iloc[-HORIZON - 1])
assert label.iloc[:BASELINE_WINDOW].isna().all()          # trailing window incomplete
assert set(label.dropna().unique()) <= {0.0, 1.0}
assert expansion_label(pd.Series([2.0, 1.0, np.nan])).tolist()[:2] == [1.0, 0.0] and np.isnan(expansion_label(pd.Series([np.nan]))[0])

# --- live scorer scores the same thing the model is trained on ---
# Build bars like Alpaca's daily bars (timestamp 04:00 on the trading date, consecutive weekdays).
dates = pd.bdate_range("2026-01-05", periods=N) + pd.Timedelta(hours=4)
priced = pd.DataFrame({"close": close.values}, index=dates)

def run_row(t):
    """A pipeline run that saw bars up to index t: it runs mid-session the next trading day,
    when the bar for day t+1 already exists in the data but was NOT in the features."""
    day = dates[t + 1].normalize()
    return {"symbol": "SPY", "run_ts": pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=19), "vol_expansion_proba": 0.3}

ts = [30, 49, 50, 59, 60, 80]
scored = vol_calibration_report(pd.DataFrame([run_row(t) for t in ts]), lambda s: priced)
assert len(scored) == len(ts)
assert scored["realized_expansion"].tolist() == [bool(label.iloc[t]) for t in ts], scored["realized_expansion"].tolist()
assert scored["realized_expansion"].tolist() == [False, False, True, True, False, False]

# Not enough forward data yet => not scored (not silently scored as "no expansion").
tail = vol_calibration_report(pd.DataFrame([run_row(N - 6)]), lambda s: priced)
assert tail.empty
# Run before the first bar: skipped, no negative-index wraparound.
early = pd.DataFrame([{"symbol": "SPY", "run_ts": pd.Timestamp("2025-12-01", tz="UTC"), "vol_expansion_proba": 0.3}])
assert vol_calibration_report(early, lambda s: priced).empty
print("vol label ok")
