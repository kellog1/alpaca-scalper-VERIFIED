"""Daily-bar swing breakout signal, built on the existing scalper indicators.

Bars must be dicts with date/open/high/low/close/volume, sorted oldest-first
(scalper.indicators.ema seeds off the earliest values and walks forward).
"""
from dataclasses import dataclass
from typing import Optional

from scalper.indicators import atr, avg_volume, ema, rolling_high

from webull_swing.config import STRATEGY


@dataclass
class Signal:
    symbol: str
    price: float
    stop: float
    target: float
    reason: str


def breakout_signal(symbol: str, bars: list[dict], params: dict = STRATEGY) -> Optional[Signal]:
    """Evaluate the most recent bar for a swing breakout entry.

    Entry: today's close breaks the prior N-day high, closes above the
    trend EMA, and volume confirms (> volume_mult x its 20d average).
    """
    needed = max(params["breakout_lookback"], params["trend_ema_period"],
                 params["atr_period"], params["volume_avg_period"]) + 1
    if len(bars) < needed:
        return None

    today = bars[-1]
    closes = [b["close"] for b in bars]

    prior_high = rolling_high(bars, params["breakout_lookback"])
    trend_ema = ema(closes, params["trend_ema_period"])
    vol_avg = avg_volume(bars, params["volume_avg_period"])
    atr_val = atr(bars, params["atr_period"])
    if None in (prior_high, trend_ema, vol_avg, atr_val) or atr_val == 0:
        return None

    if not (today["close"] > prior_high and today["close"] > trend_ema
            and today["volume"] > params["volume_mult"] * vol_avg):
        return None

    price = today["close"]
    stop = price - params["atr_stop_mult"] * atr_val
    target = price + params["atr_target_mult"] * atr_val
    reason = (f"{params['breakout_lookback']}d breakout above {prior_high:.2f}, "
              f"vol {today['volume'] / vol_avg:.1f}x avg, above "
              f"{params['trend_ema_period']}EMA ({trend_ema:.2f})")
    return Signal(symbol, price, stop, target, reason)


def check_exit(entry_price: float, stop: float, target: float, bars: list[dict],
                entry_index: int, max_hold_days: int) -> Optional[str]:
    """Check bars since entry for a stop/target/time exit. Returns the exit
    reason ("stop", "target", "time") or None if still open. Assumes the
    position was opened at bars[entry_index]'s close."""
    held_bars = bars[entry_index + 1:]
    for i, b in enumerate(held_bars):
        if b["low"] <= stop:
            return "stop"
        if b["high"] >= target:
            return "target"
        if i + 1 >= max_hold_days:
            return "time"
    return None
