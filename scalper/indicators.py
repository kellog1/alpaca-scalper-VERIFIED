"""Lightweight rolling indicators computed on in-memory bar deques.

Bars are dicts: {ts, open, high, low, close, volume}.
All functions return None until enough data exists — callers must handle that.
"""
from collections import deque
from typing import Optional


def ema(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # seed with SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars: list, period: int) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(period + 1):-1], bars[-period:]):
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        trs.append(tr)
    return sum(trs) / period


def avg_volume(bars: list, period: int) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    vols = [b["volume"] for b in bars[-(period + 1):-1]]
    return sum(vols) / period


def rolling_high(bars: list, lookback: int) -> Optional[float]:
    """Highest high of the `lookback` bars BEFORE the current bar."""
    if len(bars) < lookback + 1:
        return None
    return max(b["high"] for b in bars[-(lookback + 1):-1])


def rolling_low(bars: list, lookback: int) -> Optional[float]:
    if len(bars) < lookback + 1:
        return None
    return min(b["low"] for b in bars[-(lookback + 1):-1])


def vwap(bars: list) -> Optional[float]:
    """Session VWAP: volume-weighted typical price, computed only over bars
    from the same (UTC) session date as the most recent bar. This resets the
    accumulation daily, matching how VWAP is defined on trading platforms.
    """
    if not bars:
        return None
    session_date = bars[-1]["ts"].date()
    cum_pv = cum_vol = 0.0
    for b in reversed(bars):
        if b["ts"].date() != session_date:
            break
        typical = (b["high"] + b["low"] + b["close"]) / 3
        cum_pv += typical * b["volume"]
        cum_vol += b["volume"]
    return cum_pv / cum_vol if cum_vol > 0 else None


class SessionVWAP:
    """Running session VWAP accumulator (O(1) per bar, unaffected by buffer
    length). Resets automatically when the bar's UTC date changes."""

    def __init__(self):
        self.session_date = None
        self.cum_pv = 0.0
        self.cum_vol = 0.0

    def update(self, bar: dict) -> Optional[float]:
        d = bar["ts"].date()
        if d != self.session_date:
            self.session_date = d
            self.cum_pv = self.cum_vol = 0.0
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3
        self.cum_pv += typical * bar["volume"]
        self.cum_vol += bar["volume"]
        return self.cum_pv / self.cum_vol if self.cum_vol > 0 else None


class BarSeries:
    """Fixed-length bar history per symbol."""

    def __init__(self, maxlen: int):
        self.bars: deque[dict] = deque(maxlen=maxlen)

    def add(self, bar: dict):
        self.bars.append(bar)

    def as_list(self) -> list[dict]:
        return list(self.bars)

    def closes(self) -> list[float]:
        return [b["close"] for b in self.bars]
