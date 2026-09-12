"""Watchlist and strategy parameters for the AI-infrastructure swing agent."""

# Full-stack AI infrastructure, kept short on purpose: with a small account,
# diversifying across too many names leaves each position too small to be
# worth the spread. Skip mega-cap cloud (MSFT/GOOGL/AMZN) - too low-beta to
# produce clean swing breakouts.
WATCHLIST = ["NVDA", "AVGO", "VRT", "ANET", "MU"]

STRATEGY = {
    "breakout_lookback": 20,   # entry: close breaks the prior N-day high
    "trend_ema_period": 50,    # entry filter: close must be above this EMA
    "atr_period": 14,
    "volume_avg_period": 20,
    "volume_mult": 1.5,        # entry: volume must exceed 1.5x its 20d avg
    "atr_stop_mult": 1.5,      # stop = entry - 1.5x ATR
    "atr_target_mult": 3.0,    # target = entry + 3x ATR (2:1 reward:risk)
    "max_hold_days": 20,       # time-based exit if neither stop nor target hit
}

RISK = {
    "risk_per_trade_pct": 0.02,   # risk ~2% of settled cash per new position
    "max_concurrent_positions": 3,  # keeps individual size meaningful on $500-ish
}
