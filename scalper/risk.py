"""Risk manager for the aggressive profile.

Responsibilities:
  - position sizing from stop distance (2.5% equity risk per trade)
  - intraday leverage cap (never exceed 4x buying power exposure)
  - max 6 concurrent positions
  - daily loss limit (-6% halts trading for the day)
  - profit lock (+5% on the day -> tighten all stops to breakeven)
  - session windows (active open + power hour, scaled-down midday,
    flatten N minutes before close)
"""
import logging
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("risk")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class RiskManager:
    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.risk_pct = r["risk_per_trade_pct"]
        self.max_leverage = r["max_leverage"] if r["use_intraday_leverage"] else 1.0
        self.max_positions = r["max_concurrent_positions"]
        self.daily_loss_limit = r["daily_loss_limit_pct"]
        self.profit_lock_pct = r["profit_lock_pct"]

        s = cfg["sessions"]
        self.tz = ZoneInfo(s["timezone"])
        self.open_start = _parse_hhmm(s["open_trading_start"])
        self.open_end = _parse_hhmm(s["open_trading_end"])
        self.ph_start = _parse_hhmm(s["power_hour_start"])
        self.ph_end = _parse_hhmm(s["power_hour_end"])
        self.midday_scale = s["midday_size_scale"]
        self.flatten_min = s["flatten_before_close_min"]
        self.market_close = time(16, 0)

        self.day_start_equity: float | None = None
        self.halted = False
        self.profit_locked = False

    # ---- daily lifecycle -------------------------------------------------
    def start_of_day(self, equity: float):
        self.day_start_equity = equity
        self.halted = False
        self.profit_locked = False
        log.info("Day started. Equity=%.2f loss-limit=%.2f",
                 equity, equity * (1 - self.daily_loss_limit))

    def check_daily_limits(self, equity: float) -> dict:
        """Returns {'halt': bool, 'lock_profits': bool} based on day P&L."""
        if self.day_start_equity is None:
            self.start_of_day(equity)
        pnl_pct = (equity - self.day_start_equity) / self.day_start_equity

        if not self.halted and pnl_pct <= -self.daily_loss_limit:
            self.halted = True
            log.warning("DAILY LOSS LIMIT HIT (%.2f%%). Trading halted.", pnl_pct * 100)

        lock = False
        if not self.profit_locked and pnl_pct >= self.profit_lock_pct:
            self.profit_locked = True
            lock = True
            log.info("PROFIT LOCK triggered at +%.2f%%: tightening stops to breakeven.",
                     pnl_pct * 100)
        return {"halt": self.halted, "lock_profits": lock}

    # ---- session logic ---------------------------------------------------
    def session_state(self, now: Optional[datetime] = None) -> str:
        """'active' | 'midday' | 'flatten' | 'closed'"""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        t = now.time()
        flatten_t = time(self.market_close.hour,
                         self.market_close.minute) if self.flatten_min == 0 else \
            (datetime.combine(now.date(), self.market_close)
             .replace(tzinfo=self.tz))
        flatten_start = (datetime.combine(now.date(), self.market_close, self.tz)
                         - timedelta(minutes=self.flatten_min)).time()

        if t >= flatten_start and t < self.market_close:
            return "flatten"
        if t >= self.market_close or t < time(9, 30):
            return "closed"
        if self.open_start <= t < self.open_end or self.ph_start <= t < self.ph_end:
            return "active"
        if t < self.open_start:
            return "closed"  # skip first 5 minutes after open
        return "midday"

    # ---- sizing ----------------------------------------------------------
    def position_size(self, equity: float, buying_power: float,
                      entry: float, stop: float,
                      open_position_value: float,
                      session: str) -> int:
        """Shares to buy/short, risk-based, leverage- and session-aware."""
        if self.halted:
            return 0
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return 0

        risk_dollars = equity * self.risk_pct
        if session == "midday":
            risk_dollars *= self.midday_scale
        qty = int(risk_dollars / stop_dist)

        # Leverage / exposure cap: total position value <= max_leverage * equity
        max_exposure = self.max_leverage * equity
        remaining = max(0.0, max_exposure - open_position_value)
        qty = min(qty, int(remaining / entry), int(buying_power / entry))
        return max(qty, 0)

    def can_open(self, open_positions: int, session: str) -> bool:
        if self.halted or session in ("flatten", "closed"):
            return False
        return open_positions < self.max_positions
