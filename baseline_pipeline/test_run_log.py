"""Offline checks for the once-per-UTC-day pipeline log: python test_run_log.py"""
import os
import tempfile
import pandas as pd
import baseline_pipeline as bp

path = os.path.join(tempfile.mkdtemp(), "data", "pipeline_log.csv")
bp.PIPELINE_LOG_PATH = path
detail = pd.DataFrame([
    {"symbol": "SPY", "entry": 100.0, "stop": 95.0, "vol_expansion_proba": 0.3, "adj_risk_pct": 0.004,
     "shares": 10, "position_dollars": 1000.0, "capped_by": "risk", "allowed": True, "reason": None},
    {"symbol": "QQQ", "entry": 200.0, "stop": 190.0, "vol_expansion_proba": 0.2, "adj_risk_pct": 0.004,
     "shares": 5, "position_dollars": 1000.0, "capped_by": "risk", "allowed": True, "reason": None},
])
rows = lambda: len(pd.read_csv(path))

assert not bp.already_logged_for_day("2026-10-08T13:05:00+00:00")        # no file yet
assert bp.log_run(detail, run_ts="2026-10-08T13:05:00+00:00") is True
assert rows() == 2
# Later runs the same UTC day (the workflow fires several times) add nothing...
assert bp.log_run(detail, run_ts="2026-10-08T19:02:00+00:00") is False
assert bp.log_run(detail, run_ts="2026-10-08T23:59:59+00:00") is False
assert rows() == 2
# ...the next UTC day logs again (new bars => new predictions).
assert bp.log_run(detail, run_ts="2026-10-09T00:00:01+00:00") is True
assert rows() == 4
# Timestamps are compared in UTC: this offset time is 2026-10-09T02:30Z, same day as the row above.
assert bp.log_run(detail, run_ts="2026-10-08T22:30:00-04:00") is False
# Naive timestamps are treated as UTC.
assert bp.log_run(detail, run_ts="2026-10-09T05:00:00") is False
assert bp.log_run(detail, run_ts="2026-10-10T05:00:00") is True
assert rows() == 6
# The file stays well-formed: one header, run_ts first, consistent columns.
df = pd.read_csv(path)
assert df.columns[0] == "run_ts" and df["symbol"].tolist() == ["SPY", "QQQ"] * 3
assert df["run_ts"].nunique() == 3
print("run log ok")
