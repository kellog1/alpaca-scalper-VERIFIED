"""
Standalone daily runner — YOU own this script and its execution.
----------------------------------------------------------------
Run this on your own machine or CI runner (e.g. Windows Task Scheduler or
a GitHub Actions cron) to get genuinely unattended signal generation AND
order submission. This is the one piece Claude itself will never do —
running it, and scheduling it, is your action, using your own Alpaca
keys, not something any Claude session triggers.

WHAT IT REUSES (unchanged, already-tested logic):
  - feature_extractor.py   — engineered features
  - ml_ensemble_vol.py     — the one validated signal (vol-expansion model),
                              used only as a risk-sizing overlay
  - risk_manager.py        — position sizing, ATR stops, portfolio guardrails
  - order_router.py        — order payload construction + reconciliation
  - baseline_pipeline.py   — ties the above together (build_targets)
  - alpaca_client.py       — real HTTP to Alpaca (data + trading)

SETUP (one time):
  1. pip install -r requirements.txt   (pandas, numpy, xgboost, requests)
  2. In the Alpaca dashboard, rotate your API keys if they were ever
     pasted anywhere insecurely, and set the new ones as environment
     variables — do NOT put them in this file:
       Windows:      setx ALPACA_API_KEY_ID "your_new_key"
                     setx ALPACA_API_SECRET_KEY "your_new_secret"
       macOS/Linux:  export ALPACA_API_KEY_ID="your_new_key"
                     export ALPACA_API_SECRET_KEY="your_new_secret"
       GitHub Actions: add as repo secrets (Settings > Secrets and
                     variables > Actions), then reference them as env:
                     in your workflow step.
  3. Keep all these .py files in the same folder.
  4. First run with DRY_RUN=True (default below) — read the printed output,
     confirm the orders look sane, BEFORE ever setting DRY_RUN=False.
  5. Once you trust it, set DRY_RUN=False (below, or via the
     RUN_DAILY_DRY_RUN=false environment variable) and schedule it.

WHAT IT DOES NOT DO:
  - No self-modification. It runs the exact strategy logic that's in these
    files as of today. If you want to change parameters, edit the files
    yourself and review the change — same "no reactive daily tuning"
    discipline this project was built around, just now enforced by the
    fact that nothing in here rewrites itself.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
import pandas as pd

import alpaca_client
from baseline_pipeline import build_targets, log_run, BASKET, PIPELINE_LOG_PATH
from ml_ensemble_vol import SYMBOLS as VOL_TRAIN_SYMBOLS, REFERENCE
from order_router import reconcile_positions, intents_to_payloads

DRY_RUN = os.environ.get("RUN_DAILY_DRY_RUN", "true").lower() != "false"
ORDER_LOG_PATH = "data/order_submission_log.csv"


def log_submission(results: list[dict], run_ts: str) -> None:
    rows = [{"run_ts": run_ts, **r} for r in results]
    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(ORDER_LOG_PATH) or ".", exist_ok=True)
    write_header = not os.path.exists(ORDER_LOG_PATH)
    out.to_csv(ORDER_LOG_PATH, mode="a", header=write_header, index=False)


def main():
    run_ts = datetime.now(timezone.utc).isoformat()
    print(f"=== run_daily.py — {run_ts} — DRY_RUN={DRY_RUN} ===\n")

    all_symbols = sorted(set(VOL_TRAIN_SYMBOLS) | set(BASKET) | {REFERENCE})
    print(f"Fetching live daily bars for {len(all_symbols)} symbols from Alpaca...")
    price_data = alpaca_client.get_daily_bars(all_symbols)
    missing = [s for s in all_symbols if s not in price_data]
    if missing:
        print(f"WARNING: no data returned for {missing} — continuing without them "
              f"(vol-model training and any basket symbol among these will be degraded).")

    print("Fetching live account state...")
    account = alpaca_client.get_account()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    current_positions = alpaca_client.get_positions()
    print(f"  equity=${equity:,.2f}  buying_power=${buying_power:,.2f}  "
          f"current_positions={current_positions}")

    print("\nComputing target positions (vol-model risk overlay + ATR stops)...")
    targets, detail = build_targets(equity, buying_power, current_positions, price_data=price_data)
    pd.set_option("display.width", 140)
    print(detail.to_string(index=False))
    log_run(detail, run_ts=run_ts)

    intents = reconcile_positions(current_positions, targets)
    if not intents:
        print("\nNo orders needed — current positions already match targets.")
        return

    payloads = intents_to_payloads(intents)
    print(f"\n{len(intents)} order(s) to reconcile toward targets:")
    for i in intents:
        print(f"  {i.action}: {i.side} {i.qty} {i.symbol} (stop={i.stop_price}) — {i.reason}")

    if DRY_RUN:
        print("\nDRY_RUN=True — NOT submitting orders. Payloads that WOULD be sent:")
        for p in payloads:
            print(f"  {p}")
        print("\nSet RUN_DAILY_DRY_RUN=false (env var) once you've reviewed enough of "
              "these dry runs to trust them.")
        return

    print("\nDRY_RUN=False — submitting orders to Alpaca paper account...")
    results = []
    for p in payloads:
        res = alpaca_client.submit_order(p)
        print(f"  {p['symbol']}: status={res['status']}")
        results.append(res)
    log_submission(results, run_ts)
    print(f"\nSubmission results logged to {ORDER_LOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)
        sys.exit(1)
