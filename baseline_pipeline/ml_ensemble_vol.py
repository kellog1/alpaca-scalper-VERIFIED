"""
Phase 7b: Volatility-forecasting ML model
-------------------------------------------
Pivot from predicting return DIRECTION (shown to have no real edge: mean AUC 0.521 on
4.75yrs/37 symbols, see ml_ensemble_v2.py in the Claude.ai Project) to predicting
VOLATILITY EXPANSION — a much better-documented, more tractable phenomenon
(volatility clustering).

Label: will realized volatility over the next `horizon` days be meaningfully higher
than the current trailing realized vol? This is useful for:
  - options strategies (the paper account already runs covered calls) — sizing strikes,
    deciding whether to sell or buy premium
  - position sizing / stop placement generally

Same feature panel, same walk-forward validation discipline as the direction model —
no shortcuts, since this is explicitly headed toward eventually trading real capital.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from xgboost import XGBClassifier
from sklearn.metrics import roc_auc_score, accuracy_score

from feature_extractor import extract_features
from vol_label import vol_ratio, expansion_label, HORIZON, BASELINE_WINDOW

SYMBOLS = ['SPY','QQQ','MSFT','INTC','AMD','MARA','TSLA','NVDA','SOXL','AAPL','JNJ',
           'WBD','MPC','ARE','WDAY','ABNB','TTWO','KHC','PLD','IP','BA','PCG','SOLV',
           'AXON','COHR','COIN','CRWD','DG','ECHO','EXR','FCX','KR','LEN','MRNA','RJF',
           'SLB','SOXS']
REFERENCE = "SPY"
# HORIZON / BASELINE_WINDOW come from vol_label.py (shared with trade_analytics).


def load_data():
    dfs = {}
    for s in SYMBOLS:
        df = pd.read_csv(f"data/{s}.csv", parse_dates=["date"]).set_index("date")
        dfs[s] = df.sort_index()
    return dfs


def build_vol_label(df: pd.DataFrame, horizon: int = HORIZON, baseline_window: int = BASELINE_WINDOW):
    """
    Label = 1 if realized vol over the NEXT `horizon` days (t+1..t+horizon) exceeds the
    trailing `baseline_window`-day realized vol (ending at t) by more than 10%, else 0.
    Rows whose forward window hasn't elapsed yet are NaN (unknown), not 0.
    The definition lives in vol_label.py so the live calibration scorer uses the same one.
    """
    ratio = vol_ratio(df["close"], horizon, baseline_window)
    return expansion_label(ratio), ratio


def build_panel(dfs):
    ref_df = dfs[REFERENCE]
    rows = []
    for sym, df in dfs.items():
        feats = extract_features(df, reference=ref_df, symbol=sym)
        label, vol_ratio = build_vol_label(df)
        panel = feats.copy()
        panel["label"] = label
        panel["vol_ratio"] = vol_ratio
        panel["symbol"] = sym
        panel["close"] = df["close"]
        rows.append(panel)
    full = pd.concat(rows).reset_index().rename(columns={"index": "date"})
    return full


def walk_forward_eval(panel: pd.DataFrame, n_splits: int = 10):
    feature_cols = [
        c for c in panel.columns
        if c not in ("date", "label", "vol_ratio", "symbol", "close")
    ]
    dates = sorted(panel["date"].unique())
    usable_dates = dates[210:-HORIZON]
    chunk_size = max(len(usable_dates) // (n_splits + 1), 1)

    for i in range(1, n_splits + 1):
        split_idx = i * chunk_size
        if split_idx >= len(usable_dates):
            break
        train_end_date = usable_dates[split_idx]
        test_end_idx = min(split_idx + chunk_size, len(usable_dates) - 1)
        test_end_date = usable_dates[test_end_idx]

        train = panel[panel["date"] < train_end_date].dropna(subset=feature_cols + ["label"])
        test = panel[
            (panel["date"] >= train_end_date) & (panel["date"] < test_end_date)
        ].dropna(subset=feature_cols + ["label"])

        if len(train) < 200 or len(test) < 20:
            continue

        X_train, y_train = train[feature_cols], train["label"].astype(int)
        X_test, y_test = test[feature_cols], test["label"].astype(int)

        model = XGBClassifier(
            n_estimators=150, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss", random_state=42,
        )
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)[:, 1]
        preds = (proba > 0.5).astype(int)

        auc = roc_auc_score(y_test, proba) if y_test.nunique() > 1 else np.nan
        acc = accuracy_score(y_test, preds)

        test = test.copy()
        test["pred_proba"] = proba
        test["pred"] = preds
        summary = {
            "fold": i, "train_n": len(train), "test_n": len(test),
            "test_start": str(train_end_date.date()),
            "auc": auc, "accuracy": acc,
            "test_label_mean": y_test.mean(),
        }
        yield model, test, summary


if __name__ == "__main__":
    dfs = load_data()
    panel = build_panel(dfs)
    print(f"Panel shape: {panel.shape}, symbols: {len(panel['symbol'].unique())}")
    print(f"Date range: {panel['date'].min().date()} -> {panel['date'].max().date()}")
    print(f"Label balance (overall, vol expansion >10%): {panel['label'].mean():.3f} positive")

    all_test_frames = []
    fold_summaries = []
    last_model = None
    for model, test_frame, summary in walk_forward_eval(panel, n_splits=10):
        all_test_frames.append(test_frame)
        fold_summaries.append(summary)
        last_model = model
        print(summary)

    if not all_test_frames:
        print("Not enough data for walk-forward folds.")
    else:
        all_tests = pd.concat(all_test_frames)
        print("\n=== Walk-forward aggregate (out-of-sample, chronological, 10 folds) ===")
        print(f"Mean AUC: {np.nanmean([s['auc'] for s in fold_summaries]):.3f}")
        print(f"Mean accuracy: {np.mean([s['accuracy'] for s in fold_summaries]):.3f}")
        print(f"Baseline (always predict majority class): "
              f"{max(all_tests['label'].mean(), 1 - all_tests['label'].mean()):.3f}")

        feature_cols = [c for c in panel.columns if c not in ("date","label","vol_ratio","symbol","close")]
        importances = pd.Series(last_model.feature_importances_, index=feature_cols).sort_values(ascending=False)
        print("\n=== Feature importances (last fold's model) ===")
        print(importances.head(12))

        # Precision/recall at higher confidence thresholds — for a vol-expansion warning
        # system, false negatives (missing an expansion) matter more than false positives.
        print("\n=== Precision at various confidence thresholds (predicting vol expansion) ===")
        for thresh in (0.5, 0.55, 0.6, 0.65, 0.7):
            sub = all_tests[all_tests["pred_proba"] > thresh]
            if len(sub) == 0:
                continue
            precision = (sub["label"] == 1).mean()
            recall = len(sub[sub["label"] == 1]) / all_tests["label"].sum()
            print(f"  thresh={thresh}: n={len(sub)}, precision={precision:.3f}, recall={recall:.3f}")
