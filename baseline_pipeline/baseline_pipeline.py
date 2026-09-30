"""
Baseline automated pipeline (decoupling infrastructure from alpha)
---------------------------------------------------------------------
Neither the ML direction model (mean AUC 0.521) nor the rules-based trend
signal (underperformed buy-and-hold by 3.6 bps/day) showed a real edge.
Rather than wait on signal research to validate the automation pipeline,
this runs a simple, honest baseline strategy end-to-end through the real
components: risk_manager.py for sizing/stops and order_router.py for
execution — so the MECHANICS (signal -> size -> stop -> order -> account
reconciliation) get proven out with real paper fills now, while signal
research continues separately.

Baseline strategy (deliberately not claiming edge):
  - Fixed, small, liquid basket: SPY, QQQ, AAPL, MSFT, NVDA
  - Always target a long position in each, sized by fixed-fractional risk
    (risk_manager.position_size), stopped by ATR (risk_manager.atr_stop)
  - The one MODEL WITH A PROVEN EDGE (ml_ensemble_vol.py's volatility-
    expansion classifier) is used exactly as validated: as a risk overlay.
    It's trained on the full 37-symbol history and used to predict each
    basket symbol's current probability of a volatility expansion, which
    shrinks the risk budget via risk_manager.vol_adjusted_risk_pct()
    rather than picking direction.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
import pandas as pd
from xgboost import XGBClassifier

from feature_extractor import extract_features
from risk_manager import position_size, atr_stop, vol_adjusted_risk_pct, PortfolioConstraints
from order_router import reconcile_positions, intents_to_payloads, get_positions_js_snippet, place_orders_js_snippet
from ml_ensemble_vol import SYMBOLS as VOL_TRAIN_SYMBOLS, REFERENCE, build_vol_label

BASKET = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
# Sizing is meant to be RISK-bound so the vol overlay (which scales the risk
# budget) actually changes share counts. With 1% base risk and a 10% cap, the
# cap bound every name and the overlay was inert (see first dry-run logs).
# 5 names x 20% cap = at most 100% gross, i.e. still no leverage.
BASE_RISK_PCT = 0.005         # 0.5% of equity risked per name, before vol adjustment
MAX_POSITION_PCT = 0.20       # no single name over 20% of equity


def _load(symbol: str) -> pd.DataFrame:
    return pd.read_csv(f"data/{symbol}.csv", parse_dates=["date"]).set_index("date").sort_index()


def fit_live_vol_model(price_data: dict | None = None) -> tuple[XGBClassifier, list[str]]:
    """
    Fits the volatility-expansion classifier on ALL available history
    (not walk-forward — this is for live prediction, not backtesting; the
    walk-forward numbers in ml_ensemble_vol.py already established this
    model generalizes out-of-sample).

    price_data: optional {symbol: DataFrame} to use instead of reading the
    local data/ CSVs — lets a caller (e.g. a standalone script pulling
    fresh bars from a live data source) reuse this exact, already-tested
    logic without going through local files.
    """
    dfs = price_data if price_data is not None else {s: _load(s) for s in VOL_TRAIN_SYMBOLS}
    ref_df = dfs[REFERENCE]
    rows = []
    for sym, df in dfs.items():
        feats = extract_features(df, reference=ref_df, symbol=sym)
        label, _ = build_vol_label(df)
        panel = feats.copy()
        panel["label"] = label
        rows.append(panel)
    full = pd.concat(rows)
    feature_cols = [c for c in full.columns if c != "label"]
    train = full.dropna(subset=feature_cols + ["label"])

    model = XGBClassifier(
        n_estimators=150, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, eval_metric="logloss", random_state=42,
    )
    model.fit(train[feature_cols], train["label"])
    return model, feature_cols


def build_targets(equity: float, buying_power: float, current_positions: dict, price_data: dict | None = None) -> dict:
    """
    Returns {symbol: {"qty": int, "stop_price": float}} for the basket,
    using live vol-expansion predictions to adjust risk, then
    risk_manager.position_size/atr_stop for the actual sizing and stop.

    price_data: optional {symbol: DataFrame} covering BASKET + VOL_TRAIN_SYMBOLS
    + REFERENCE — see fit_live_vol_model(). Falls back to local data/ CSVs
    when not provided.
    """
    vol_model, feature_cols = fit_live_vol_model(price_data)

    def _get(sym):
        return price_data[sym] if price_data is not None else _load(sym)

    ref_df = _get(REFERENCE)

    pc = PortfolioConstraints(max_positions=len(BASKET), max_symbol_weight=MAX_POSITION_PCT)
    # Targets are FULL desired holdings, not increments, so constraints are
    # evaluated on a fresh portfolio. Pre-registering current positions would
    # double-count them and make held names fail the weight/count checks,
    # flipping their target to zero and closing them.

    targets = {}
    detail_rows = []
    for sym in BASKET:
        df = _get(sym)
        feats = extract_features(df, reference=ref_df, symbol=sym)
        latest_feats = feats.iloc[[-1]][feature_cols]
        if latest_feats.isna().any(axis=None):
            detail_rows.append({"symbol": sym, "skipped": "NaN in latest features"})
            continue

        vol_proba = float(vol_model.predict_proba(latest_feats)[0, 1])
        adj_risk_pct = vol_adjusted_risk_pct(BASE_RISK_PCT, vol_proba)

        entry_price = float(df["close"].iloc[-1])
        stop = atr_stop(df, entry_price, direction="long")
        if stop["stop_price"] is None:
            detail_rows.append({"symbol": sym, "skipped": stop["reason"]})
            continue

        sized = position_size(
            equity, entry_price, stop["stop_price"],
            risk_pct=adj_risk_pct, max_position_pct=MAX_POSITION_PCT,
            buying_power=buying_power,
        )

        check = pc.can_open_new_position(sym, sized["position_dollars"], equity)
        detail_rows.append({
            "symbol": sym, "entry": entry_price, "stop": stop["stop_price"],
            "vol_expansion_proba": round(vol_proba, 3), "adj_risk_pct": round(adj_risk_pct, 4),
            "shares": sized["shares"], "position_dollars": sized["position_dollars"],
            "capped_by": sized["capped_by"],
            "allowed": check["allowed"], "reason": check.get("reason"),
        })

        if check["allowed"] and sized["shares"] > 0:
            targets[sym] = {"qty": sized["shares"], "stop_price": stop["stop_price"]}
            pc.register_fill(sym, sized["position_dollars"])

    return targets, pd.DataFrame(detail_rows)


PIPELINE_LOG_PATH = "data/pipeline_log.csv"


def _migrate_log_columns(new_columns: list[str]) -> None:
    """Appending rows with a new column to an existing CSV would corrupt it, so
    add missing columns first. Rows logged before `capped_by` existed were all
    bound by the position cap (verified from their sizing), so back-fill that."""
    if not os.path.exists(PIPELINE_LOG_PATH):
        return
    old = pd.read_csv(PIPELINE_LOG_PATH)
    missing = [c for c in new_columns if c not in old.columns]
    if not missing:
        return
    for c in missing:
        old[c] = "max_position_pct" if c == "capped_by" else None
    old.to_csv(PIPELINE_LOG_PATH, index=False)


def log_run(detail: pd.DataFrame, run_ts: str | None = None) -> None:
    """
    Appends this run's predictions/sizing (vol_expansion_proba, entry, stop,
    shares) to a persistent CSV log — this is what trade_analytics.py joins
    real fills against later to check calibration (did predicted vol
    expansion probability line up with what actually happened) and stop
    discipline (did the actual exit match the ATR stop that was set here).
    Kept separate from execution so the log captures every run's INTENT
    regardless of whether the orders that day ended up being submitted.
    """
    if run_ts is None:
        run_ts = datetime.now(timezone.utc).isoformat()
    out = detail.copy()
    out.insert(0, "run_ts", run_ts)
    os.makedirs(os.path.dirname(PIPELINE_LOG_PATH) or ".", exist_ok=True)
    _migrate_log_columns(list(out.columns))
    write_header = not os.path.exists(PIPELINE_LOG_PATH)
    if not write_header:   # align appended rows with the file's header, whatever order/subset this run has
        out = out.reindex(columns=list(pd.read_csv(PIPELINE_LOG_PATH, nrows=0).columns))
    out.to_csv(PIPELINE_LOG_PATH, mode="a", header=write_header, index=False)


if __name__ == "__main__":
    pd.set_option("display.width", 140)
    print("Fitting live vol-expansion model on full history and computing target positions...")
    print("(This is a DRY RUN using local data/ CSVs — for a live run, use run_daily.py instead.)\n")

    # Placeholder account state for a dry run without live fetch wired in yet.
    equity = 96_093.35
    buying_power = 375_744.63
    current_positions = {}  # filled in from a real get_positions() call before use

    targets, detail = build_targets(equity, buying_power, current_positions)
    print(detail.to_string(index=False))
    log_run(detail)
    print(f"\n(Run logged to {PIPELINE_LOG_PATH} for later validation by trade_analytics.py)")

    intents = reconcile_positions(current_positions, targets)
    print(f"\n{len(intents)} order(s) needed to reach targets:")
    for i in intents:
        print(f"  {i.action}: {i.side} {i.qty} {i.symbol} (stop={i.stop_price}) — {i.reason}")

    payloads = intents_to_payloads(intents)
    print("\n--- Order payloads ---")
    for p in payloads:
        print(p)
