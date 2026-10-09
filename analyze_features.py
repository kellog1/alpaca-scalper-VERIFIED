"""Do the signal-time features predict trade outcomes?

Usage:
    python backtest.py --breakout --days 60 --dump-trades trades.csv
    python analyze_features.py trades.csv

Prints, per feature, win rate and mean net P&L by quartile, then a
time-ordered cross-validated AUC for a shallow gradient-boosted model.
With a few hundred trades this is a screen for ideas, not evidence: an
AUC near 0.5 means the features carry no usable signal.
"""
import argparse

import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

NUMERIC = ["ema_spread_pct", "vol_ratio", "range_atr_ratio", "vwap_dev_pct",
           "hour_utc", "entries_this_trend"]
BOOL = ["htf_agree"]


def by_quartile(df, col):
    q = pd.qcut(df[col], 4, duplicates="drop")
    g = df.groupby(q, observed=True).agg(
        n=("pnl", "size"), win_rate=("win", "mean"), mean_pnl=("pnl", "mean"))
    print(f"\n{col}\n{g.round(4).to_string()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--splits", type=int, default=5)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    missing = [c for c in NUMERIC + BOOL if c not in df.columns]
    if missing:
        raise SystemExit(f"No feature columns {missing}. Re-run the backtest "
                         "with --breakout (mean-reversion trades have none).")
    df = df.sort_values("entry_ts").reset_index(drop=True)
    df["win"] = (df["pnl"] > 0).astype(int)
    print(f"{len(df)} trades, win rate {df['win'].mean():.1%}, "
          f"mean net pnl {df['pnl'].mean():.2f}")

    for col in NUMERIC:
        if df[col].notna().sum() >= 8 and df[col].nunique() > 1:
            by_quartile(df, col)
    g = df.groupby("htf_agree", dropna=False).agg(
        n=("pnl", "size"), win_rate=("win", "mean"), mean_pnl=("pnl", "mean"))
    print(f"\nhtf_agree\n{g.round(4).to_string()}")

    X = df[NUMERIC].copy()
    X["htf_agree"] = df["htf_agree"].map({True: 1.0, False: 0.0})
    X = X.fillna(X.median(numeric_only=True)).fillna(0)
    y = df["win"]
    if len(df) < 50 or y.nunique() < 2:
        print("\nToo few trades for a model (need >= 50 with both outcomes).")
        return
    aucs = []
    for tr, te in TimeSeriesSplit(n_splits=args.splits).split(X):
        if y.iloc[tr].nunique() < 2 or y.iloc[te].nunique() < 2:
            continue
        m = GradientBoostingClassifier(max_depth=2, n_estimators=100,
                                       learning_rate=0.05, random_state=0)
        m.fit(X.iloc[tr], y.iloc[tr])
        aucs.append(roc_auc_score(y.iloc[te], m.predict_proba(X.iloc[te])[:, 1]))
    if aucs:
        print(f"\nTime-ordered CV AUC: {sum(aucs)/len(aucs):.3f} "
              f"(folds: {', '.join(f'{a:.2f}' for a in aucs)}) - 0.50 = no signal")


if __name__ == "__main__":
    main()
