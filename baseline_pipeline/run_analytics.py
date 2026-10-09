"""
Daily read-only analytics — reports on real fills vs. the pipeline's own log.
-----------------------------------------------------------------------------
Never submits or cancels anything: it only calls alpaca_client.get_fills()
and get_daily_bars(). Safe to run unattended (see .github/workflows/
daily-analytics.yml). Produces numbers only; per the "no reactive daily
tuning" decision, any parameter change is a separate, deliberate decision.

Fills are restricted to BASKET symbols AND to fills at/after the first
pipeline log row. The account also holds older fills from the intraday
scalper (it traded SPY/QQQ/NVDA before they were reserved for this
pipeline); activity records carry no strategy tag, so time is the cutoff.

Output: printed to stdout and written to data/analytics_report.md.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
import pandas as pd

import alpaca_client
import trade_analytics as ta
from baseline_pipeline import BASKET

REPORT_PATH = "data/analytics_report.md"
MIN_SCORED_FOR_CALIBRATION = 30   # below this, calibration buckets are noise


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}\n"


def _table(df: pd.DataFrame) -> str:
    return "```\n" + df.to_string(index=False) + "\n```" if len(df) else "_No data yet._"


def select_pipeline_fills(fills: pd.DataFrame, log: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Basket fills at/after the first pipeline run. Returns (fills, n_excluded_earlier)."""
    if fills.empty:
        return fills, 0
    basket = fills[fills["symbol"].isin(BASKET)]
    since = log["run_ts"].min()
    keep = basket[basket["filled_at"] >= since].reset_index(drop=True)
    return keep, len(basket) - len(keep)


def build_report(fills: pd.DataFrame, log: pd.DataFrame, price_loader, excluded: int = 0) -> str:
    parts = [f"# Trade analytics — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n",
             f"Pipeline log rows: {len(log)} | pipeline fills: {len(fills)} "
             f"(excluded {excluded} earlier basket-symbol fills from before the first pipeline run "
             f"{log['run_ts'].min():%Y-%m-%d %H:%M} UTC)\n"]

    if fills.empty:
        parts.append(_section("Fills", "_No basket fills yet — slippage, stop discipline and P&L "
                                       "need live-paper trades. Calibration below uses the log only._"))
    else:
        closed, open_pos = ta.fifo_pnl(fills)
        slip = ta.slippage_report(fills, log)
        if len(slip):
            summary = (f"n={len(slip)}  mean={slip['slippage_pct'].mean():.4%}  "
                       f"max={slip['slippage_pct'].max():.4%}\n\n")
            parts.append(_section("Slippage (fill vs. price assumed at sizing)", summary + _table(slip)))
        else:
            parts.append(_section("Slippage", "_No buy fills matched to a logged run._"))

        if len(closed):
            parts.append(_section("Realized P&L (FIFO)",
                                  f"closed lots={len(closed)}  total=${closed['pnl'].sum():,.2f}\n\n"
                                  + _table(closed)))
            sd = ta.stop_discipline_report(closed, log)
            flagged = int((sd["flag"] != "ok").sum()) if len(sd) else 0
            parts.append(_section("Stop discipline", f"flagged={flagged} of {len(sd)}\n\n" + _table(sd)))
        else:
            parts.append(_section("Realized P&L / stop discipline", "_No closed trades yet._"))
        parts.append(_section("Open positions (from fills)", str(open_pos) if open_pos else "_None._"))

    scored = ta.vol_calibration_report(log, price_loader)
    if len(scored) < MIN_SCORED_FOR_CALIBRATION:
        parts.append(_section("Vol-model calibration",
                              f"_Only {len(scored)} scored predictions (need {MIN_SCORED_FOR_CALIBRATION}+; "
                              "each needs ~10 trading days to elapse). Not interpretable yet._"))
    else:
        parts.append(_section("Vol-model calibration (predicted vs. actual expansion rate)",
                              _table(ta.calibration_summary(scored))))
    return "\n".join(parts)


def main():
    if not os.path.exists(ta.PIPELINE_LOG_PATH):
        print(f"No pipeline log at {ta.PIPELINE_LOG_PATH} yet — nothing to analyze.")
        return
    log = ta.load_pipeline_log()

    print("Fetching fills (read-only)...")
    fills, excluded = select_pipeline_fills(ta.parse_fills(alpaca_client.get_fills()), log)

    print("Fetching daily bars for calibration (read-only)...")
    bars = alpaca_client.get_daily_bars(sorted(log["symbol"].unique()))
    report = build_report(fills, log, lambda sym: bars[sym], excluded)

    print(report)
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)
        sys.exit(1)
