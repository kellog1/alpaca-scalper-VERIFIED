"""Offline checks for earnings annotation: python test_earnings.py"""
from datetime import date
import pandas as pd
from earnings import load_earnings, annotate_earnings, EARNINGS_PATH

today = date(2026, 10, 7)
det = pd.DataFrame({"symbol": ["SPY", "AAPL", "MSFT", "NVDA", "ZZZ"]})
e = {"SPY": None, "AAPL": date(2026, 10, 29), "MSFT": date(2026, 10, 9), "NVDA": date(2026, 10, 1)}
out, notes = annotate_earnings(det, e, today)
r = out.set_index("symbol")
assert r.loc["AAPL", "next_earnings"] == "2026-10-29" and r.loc["AAPL", "days_to_earnings"] == 22
assert r.loc["MSFT", "days_to_earnings"] == 2
assert r.loc["SPY", "next_earnings"] == "" and r.loc["ZZZ", "next_earnings"] == ""   # ETF / unknown -> blank
assert r.loc["NVDA", "next_earnings"] == "" and r.loc["NVDA", "days_to_earnings"] == ""  # stale -> blank
assert any("EARNINGS SOON: MSFT" in n for n in notes) and any("stale for NVDA" in n for n in notes)
assert not any("AAPL" in n for n in notes)                                            # 22 days: no note
assert annotate_earnings(det, e, date(2026, 10, 29))[0].set_index("symbol").loc["AAPL", "days_to_earnings"] == 0   # day of
assert list(annotate_earnings(pd.DataFrame(columns=["symbol"]), e, today)[0].columns) == ["symbol", "next_earnings", "days_to_earnings"]
assert load_earnings("/nonexistent/file.csv") == {}                                    # missing file: no annotations
seeded = load_earnings(EARNINGS_PATH)
assert seeded["AAPL"] == date(2026, 10, 29) and seeded["SPY"] is None and set(seeded) >= {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"}
print("earnings ok")
