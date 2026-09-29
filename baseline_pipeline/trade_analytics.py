"""
Phase 6: Trade Analytics
--------------------------
Read-only, post-trade validation — no order execution here, so unlike
order_router.py/alpaca_client.py this module has no constraint on being run
automatically/unattended (it only reads account activity and the pipeline's
own prediction log, never submits anything).

Answers, after each trading day, the honest-edge questions this whole
project has been built around:
  1. Slippage       — did the actual fill price match what the pipeline
                       assumed when it sized the position (previous close)?
  2. Stop discipline — did exits actually happen at/near the ATR stop that
                       was set, or did something (gap, slippage, missed
                       exit) blow through it?
  3. Vol-model calibration — did the volatility-expansion model's predicted
                       probability, logged at entry time, actually track
                       realized forward volatility? This is the ongoing
                       version of the walk-forward check already done in
                       ml_ensemble_vol.py — same discipline, now on live
                       (paper) data instead of historical folds.
  4. Realized P&L — FIFO-matched.

Deliberately does NOT auto-adjust the strategy. It produces numbers; per
the "no reactive daily tuning" decision, any parameter change is a
separate, deliberate decision made once enough trades have accumulated —
this module just makes sure that decision is made on real evidence.

Usage once you have real fills (e.g. from alpaca_client.get_fills()):
    import alpaca_client, trade_analytics as ta, baseline_pipeline as bp
    fills = ta.parse_fills(alpaca_client.get_fills())
    log = ta.load_pipeline_log()
    print(ta.slippage_report(fills, log))
    closed, open_positions = ta.fifo_pnl(fills)
    print(ta.stop_discipline_report(closed, log))
    scored = ta.vol_calibration_report(log, bp._load)
    print(ta.calibration_summary(scored))
"""
from __future__ import annotations
import pandas as pd
import numpy as np

PIPELINE_LOG_PATH = "data/pipeline_log.csv"


# ---------------------------------------------------------------------------
# 1. Parse Alpaca fill activity records (read-only — the shape returned by
#    GET /v2/account/activities/FILL)
# ---------------------------------------------------------------------------

def parse_fills(activities: list[dict]) -> pd.DataFrame:
    rows = []
    for a in activities:
        if a.get("activity_type") != "FILL":
            continue
        rows.append({
            "symbol": a["symbol"],
            "side": a["side"],                     # buy / sell / sell_short
            "qty": float(a["qty"]),
            "price": float(a["price"]),
            "filled_at": pd.to_datetime(a["transaction_time"]),
            "order_id": a.get("order_id"),
        })
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values("filled_at").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 2. FIFO realized P&L
# ---------------------------------------------------------------------------

def fifo_pnl(fills: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    closed_trades = []
    open_lots = {}  # symbol -> list of [qty, price] (long lots)

    for _, f in fills.iterrows():
        sym, side, qty, price = f["symbol"], f["side"], f["qty"], f["price"]
        lots = open_lots.setdefault(sym, [])

        if side == "buy":
            remaining = qty
            # cover any short lots first (negative qty convention)
            while remaining > 0 and lots and lots[0][0] < 0:
                lot_qty, lot_price = lots[0]
                matched = min(remaining, -lot_qty)
                closed_trades.append({
                    "symbol": sym, "qty": matched, "entry_price": lot_price,
                    "exit_price": price, "pnl": matched * (lot_price - price),
                    "closed_at": f["filled_at"],
                })
                lots[0][0] += matched
                remaining -= matched
                if lots[0][0] == 0:
                    lots.pop(0)
            if remaining > 0:
                lots.append([remaining, price])

        elif side in ("sell", "sell_short"):
            remaining = qty
            while remaining > 0 and lots and lots[0][0] > 0:
                lot_qty, lot_price = lots[0]
                matched = min(remaining, lot_qty)
                closed_trades.append({
                    "symbol": sym, "qty": matched, "entry_price": lot_price,
                    "exit_price": price, "pnl": matched * (price - lot_price),
                    "closed_at": f["filled_at"],
                })
                lots[0][0] -= matched
                remaining -= matched
                if lots[0][0] == 0:
                    lots.pop(0)
            if remaining > 0:
                lots.append([-remaining, price])

    open_positions = {s: sum(q for q, _ in ls) for s, ls in open_lots.items() if sum(q for q, _ in ls) != 0}
    return pd.DataFrame(closed_trades), open_positions


# ---------------------------------------------------------------------------
# 3. Slippage + stop discipline — joins fills against the pipeline's own
#    prediction log (written by baseline_pipeline.py's log_run())
# ---------------------------------------------------------------------------

def load_pipeline_log(path: str = PIPELINE_LOG_PATH) -> pd.DataFrame:
    log = pd.read_csv(path, parse_dates=["run_ts"])
    log = log[log.get("allowed", True) == True].copy()
    return log


def slippage_report(fills: pd.DataFrame, log: pd.DataFrame) -> pd.DataFrame:
    """
    For each BUY fill, find the most recent prior pipeline log row for that
    symbol and compare the fill price to the entry price the pipeline
    assumed (previous close) when it sized the position.
    """
    rows = []
    buys = fills[fills["side"] == "buy"]
    for _, fill in buys.iterrows():
        candidates = log[(log["symbol"] == fill["symbol"]) & (log["run_ts"] <= fill["filled_at"])]
        if candidates.empty:
            continue
        logged = candidates.iloc[-1]
        assumed_entry = logged["entry"]
        slippage_pct = (fill["price"] - assumed_entry) / assumed_entry
        rows.append({
            "symbol": fill["symbol"], "filled_at": fill["filled_at"],
            "assumed_entry": assumed_entry, "actual_fill": fill["price"],
            "slippage_pct": round(slippage_pct, 5), "logged_stop": logged["stop"],
            "logged_vol_proba": logged["vol_expansion_proba"],
        })
    return pd.DataFrame(rows)


def stop_discipline_report(closed_trades: pd.DataFrame, log: pd.DataFrame) -> pd.DataFrame:
    """
    For each closed (losing, sell-side-exit) trade, checks the exit price
    against the stop that was logged for that entry — flags anything that
    exited materially worse than the logged stop (gap risk / slippage
    beyond what ATR sizing assumed).
    """
    rows = []
    for _, t in closed_trades.iterrows():
        candidates = log[(log["symbol"] == t["symbol"]) & (log["run_ts"] <= t["closed_at"])]
        if candidates.empty:
            continue
        logged_stop = candidates.iloc[-1]["stop"]
        breach_pct = (logged_stop - t["exit_price"]) / logged_stop if t["exit_price"] < logged_stop else 0.0
        rows.append({
            "symbol": t["symbol"], "closed_at": t["closed_at"],
            "exit_price": t["exit_price"], "logged_stop": logged_stop,
            "breached_stop_by_pct": round(breach_pct, 5),
            "flag": "STOP BREACHED (gap/slippage)" if breach_pct > 0.005 else "ok",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4. Vol-model calibration — the ongoing, live version of the walk-forward
#    check in ml_ensemble_vol.py. Needs realized forward vol, so this
#    compares each logged prediction against the ACTUAL price data once
#    enough days have passed (horizon=10 trading days, matching the model).
# ---------------------------------------------------------------------------

def vol_calibration_report(log: pd.DataFrame, price_loader, horizon: int = 10, baseline_window: int = 20) -> pd.DataFrame:
    """
    price_loader: callable(symbol) -> DataFrame with a 'close' column
    indexed by date, e.g. baseline_pipeline._load, or a wrapper around
    alpaca_client.get_daily_bars for live data.
    """
    rows = []
    for _, row in log.iterrows():
        df = price_loader(row["symbol"])
        run_date = row["run_ts"].tz_localize(None) if row["run_ts"].tzinfo else row["run_ts"]
        idx = df.index.searchsorted(run_date)
        if idx + horizon >= len(df) or idx < baseline_window:
            continue  # not enough future data yet to score this prediction
        log_ret = np.log(df["close"] / df["close"].shift(1))
        trailing_vol = log_ret.iloc[idx - baseline_window: idx].std()
        fwd_vol = log_ret.iloc[idx + 1: idx + 1 + horizon].std()
        realized_expansion = (fwd_vol / trailing_vol) > 1.10 if trailing_vol > 0 else None
        rows.append({
            "symbol": row["symbol"], "run_ts": row["run_ts"],
            "predicted_proba": row["vol_expansion_proba"],
            "realized_expansion": realized_expansion,
        })
    if not rows:
        return pd.DataFrame(columns=["symbol", "run_ts", "predicted_proba", "realized_expansion"])
    scored = pd.DataFrame(rows).dropna(subset=["realized_expansion"])
    return scored


def calibration_summary(scored: pd.DataFrame, n_buckets: int = 3) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame()
    scored = scored.copy()
    scored["bucket"] = pd.qcut(scored["predicted_proba"], q=min(n_buckets, scored["predicted_proba"].nunique()), duplicates="drop")
    return scored.groupby("bucket").agg(
        n=("realized_expansion", "size"),
        mean_predicted_proba=("predicted_proba", "mean"),
        actual_expansion_rate=("realized_expansion", "mean"),
    ).reset_index()


if __name__ == "__main__":
    import baseline_pipeline as bp
    import os

    pd.set_option("display.width", 140)

    if not os.path.exists(PIPELINE_LOG_PATH):
        print(f"No pipeline log at {PIPELINE_LOG_PATH} yet — run baseline_pipeline.py or run_daily.py first.")
    else:
        log = load_pipeline_log()
        print(f"Loaded {len(log)} logged pipeline predictions.")
        print("\nTo score against real fills, call parse_fills(alpaca_client.get_fills()) "
              "and pass the result into slippage_report()/fifo_pnl() — see the module "
              "docstring for the full usage example.")

        print("\n=== Vol-model calibration against local data/ CSVs (may be empty if the "
              "log is too recent) ===")
        scored = vol_calibration_report(log, bp._load)
        if scored.empty:
            print("Not enough elapsed time yet since the logged run(s) to score calibration.")
        else:
            print(calibration_summary(scored).to_string(index=False))
