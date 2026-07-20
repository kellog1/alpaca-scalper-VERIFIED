"""Signal engine: momentum breakout + volume spike + EMA alignment + ATR expansion,
optionally gated by session-VWAP side and higher-timeframe trend alignment.

Entry filters (all enabled must pass on the same bar):
  1. Breakout: close breaks prior N-bar high/low
  2. Volume: bar volume > volume_spike_mult x average
  3. EMA alignment: fast EMA >/< slow EMA defines trend direction
  4. Range expansion: bar range exceeds ATR multiple
  5. VWAP side (optional): long only above session VWAP, short only below
  6. HTF trend (optional): higher-timeframe close vs EMA must agree with direction

HTF implementation note: HTF bars are aggregated INTERNALLY from the base bars
this engine receives, so the filter behaves identically in live trading and in
backtest with no extra data feed. Until htf_ema_period HTF bars have accumulated
(e.g. 20 x 5min = 100 minutes), the HTF filter abstains and does NOT block
entries — set htf_ema_period lower if you want it active sooner.

Re-entry: up to `max_reentries_per_trend` continuation entries per symbol while
EMA alignment holds; counter resets when the EMA relationship flips.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .indicators import (BarSeries, SessionVWAP, ema, atr, avg_volume,
                         rolling_high, rolling_low)


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Signal:
    symbol: str
    side: Side
    price: float          # breakout close price
    atr: float            # ATR at signal time, used for TP/SL sizing
    reason: str


class _HTFAggregator:
    """Builds higher-timeframe bars from base bars for one symbol."""

    def __init__(self, htf_seconds: int, maxlen: int):
        self.interval = htf_seconds
        self.current: dict | None = None
        self.series = BarSeries(maxlen)

    def update(self, bar: dict):
        bucket = int(bar["ts"].timestamp() // self.interval)
        if self.current is None or self.current["bucket"] != bucket:
            if self.current is not None:
                self.series.add(self.current)
            self.current = {"bucket": bucket, "ts": bar["ts"],
                            "open": bar["open"], "high": bar["high"],
                            "low": bar["low"], "close": bar["close"],
                            "volume": bar["volume"]}
        else:
            c = self.current
            c["high"] = max(c["high"], bar["high"])
            c["low"] = min(c["low"], bar["low"])
            c["close"] = bar["close"]
            c["volume"] += bar["volume"]

    def trend(self, ema_period: int) -> Optional[Side]:
        """HTF trend from completed HTF bars; None while warming up."""
        closes = self.series.closes()
        e = ema(closes, ema_period)
        if e is None:
            return None
        return Side.LONG if closes[-1] > e else Side.SHORT


class SignalEngine:
    def __init__(self, cfg: dict):
        s = cfg["strategy"]
        self.lookback = s["breakout_lookback"]
        self.vol_mult = s["volume_spike_mult"]
        self.vol_period = s["volume_avg_period"]
        self.ema_fast_p = s["ema_fast"]
        self.ema_slow_p = s["ema_slow"]
        self.atr_period = s["atr_period"]
        self.atr_mult = s["atr_expansion_mult"]
        self.max_reentries = s["max_reentries_per_trend"]

        self.use_vwap_filter = s.get("use_vwap_filter", False)
        self.use_htf_filter = s.get("use_htf_trend_filter", False)
        self.htf_seconds = s.get("htf_timeframe", 300)
        self.htf_ema_p = s.get("htf_ema_period", 20)

        self._maxlen = cfg["bars"]["lookback_bars"]
        self.series: dict = {}
        self.vwaps: dict = {}
        self.htf: dict = {}
        self.trend: dict = {}
        self.entries_this_trend: dict = {}

    def _init_symbol(self, symbol: str):
        if symbol not in self.series:
            self.series[symbol] = BarSeries(self._maxlen)
            self.vwaps[symbol] = SessionVWAP()
            self.htf[symbol] = _HTFAggregator(self.htf_seconds, self._maxlen)
            self.trend[symbol] = None
            self.entries_this_trend[symbol] = 0

    def on_bar(self, symbol: str, bar: dict) -> Optional[Signal]:
        self._init_symbol(symbol)
        series = self.series[symbol]
        series.add(bar)
        vwap_val = self.vwaps[symbol].update(bar)
        self.htf[symbol].update(bar)

        bars = series.as_list()
        closes = series.closes()
        e_fast = ema(closes, self.ema_fast_p)
        e_slow = ema(closes, self.ema_slow_p)
        a = atr(bars, self.atr_period)
        v_avg = avg_volume(bars, self.vol_period)
        hi = rolling_high(bars, self.lookback)
        lo = rolling_low(bars, self.lookback)

        if None in (e_fast, e_slow, a, v_avg, hi, lo):
            return None  # base indicators warming up

        # Track trend flips to reset the re-entry counter
        new_trend = Side.LONG if e_fast > e_slow else Side.SHORT
        if new_trend != self.trend[symbol]:
            self.trend[symbol] = new_trend
            self.entries_this_trend[symbol] = 0

        # Shared filters
        volume_spike = bar["volume"] > self.vol_mult * v_avg
        range_expansion = (bar["high"] - bar["low"]) > self.atr_mult * a
        if not (volume_spike and range_expansion):
            return None

        # 1 initial entry + N re-entries per trend
        if self.entries_this_trend[symbol] >= 1 + self.max_reentries:
            return None

        close = bar["close"]

        # VWAP side filter: long above session VWAP, short below
        if self.use_vwap_filter and vwap_val is not None:
            if new_trend == Side.LONG and close < vwap_val:
                return None
            if new_trend == Side.SHORT and close > vwap_val:
                return None

        # HTF trend filter: abstains (allows) while warming up
        if self.use_htf_filter:
            htf_trend = self.htf[symbol].trend(self.htf_ema_p)
            if htf_trend is not None and htf_trend != new_trend:
                return None

        if new_trend == Side.LONG and close > hi:
            self.entries_this_trend[symbol] += 1
            return Signal(symbol, Side.LONG, close, a,
                          f"breakout>{hi:.2f} vol {bar['volume']:.0f}>{self.vol_mult}x{v_avg:.0f}")
        if new_trend == Side.SHORT and close < lo:
            self.entries_this_trend[symbol] += 1
            return Signal(symbol, Side.SHORT, close, a,
                          f"breakdown<{lo:.2f} vol {bar['volume']:.0f}>{self.vol_mult}x{v_avg:.0f}")
        return None


class MeanReversionSignalEngine:
    """Sketch: fades overextended moves back toward session VWAP on 5-min
    bars, instead of chasing breakouts on 30s bars.

    Entry: price is >= dev_threshold_pct away from session VWAP AND the move
    is decelerating (current bar closer to VWAP than the prior bar) — avoids
    fading a still-accelerating trend. One fade per extension; re-arms once
    price crosses back near VWAP.

    Drop-in replacement for SignalEngine (same on_bar(symbol, bar) ->
    Optional[Signal] interface, same Signal.atr-based TP/SL sizing), so
    execution.py/bot.py/backtest.py need no changes to try it — e.g. in
    backtest.py swap `engine = SignalEngine(cfg)` for
    `engine = MeanReversionSignalEngine(cfg)`.

    NOT YET BACKTESTED — validate before using live.
    """

    def __init__(self, cfg: dict):
        m = cfg["mean_reversion"]
        self.bar_seconds = m.get("timeframe_seconds", 300)
        self.dev_threshold = m["dev_threshold_pct"]
        self.atr_period = m["atr_period"]
        self.atr_tp_mult = m["atr_tp_mult"]
        self.atr_sl_mult = m["atr_sl_mult"]

        self._maxlen = max(cfg["bars"]["lookback_bars"], self.atr_period + 5)
        self.aggs: dict = {}
        self.vwaps: dict = {}
        self.armed: dict = {}
        self.prev_dev: dict = {}
        self.session_date: dict = {}

    def _init_symbol(self, symbol: str):
        if symbol not in self.aggs:
            self.aggs[symbol] = _HTFAggregator(self.bar_seconds, self._maxlen)
            self.vwaps[symbol] = SessionVWAP()
            self.armed[symbol] = True
            self.prev_dev[symbol] = 0.0
            self.session_date[symbol] = None

    def on_bar(self, symbol: str, bar: dict) -> Optional[Signal]:
        """Feed base bars; only acts when a 5-min bar completes."""
        self._init_symbol(symbol)
        d = bar["ts"].date()
        if d != self.session_date[symbol]:
            self.session_date[symbol] = d
            self.armed[symbol] = True  # fresh session — VWAP resets too, so re-arm
            self.prev_dev[symbol] = 0.0
        agg = self.aggs[symbol]
        prev_bucket = agg.current["bucket"] if agg.current else None
        agg.update(bar)
        if agg.current["bucket"] == prev_bucket:
            return None  # still the same 5-min bar, hasn't closed yet

        bars = agg.series.as_list()
        a = atr(bars, self.atr_period)
        if a is None:
            return None

        closed_bar = bars[-1]
        vwap_val = self.vwaps[symbol].update(closed_bar)
        if vwap_val is None:
            return None

        close = closed_bar["close"]
        dev = (close - vwap_val) / vwap_val

        if abs(dev) < self.dev_threshold * 0.3:
            self.armed[symbol] = True  # back near VWAP — re-arm for the next extension

        signal = None
        if self.armed[symbol] and abs(dev) >= self.dev_threshold:
            if dev > 0:
                signal = Signal(symbol, Side.SHORT, close, a,
                                f"fade +{dev*100:.2f}% vs VWAP {vwap_val:.2f}")
            else:
                signal = Signal(symbol, Side.LONG, close, a,
                                f"fade {dev*100:.2f}% vs VWAP {vwap_val:.2f}")
            self.armed[symbol] = False

        self.prev_dev[symbol] = dev
        return signal
