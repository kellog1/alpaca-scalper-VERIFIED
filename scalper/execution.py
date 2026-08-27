"""Execution engine.

- Bracket orders (entry + take-profit + stop-loss) so exits live server-side.
- Client-side overlays Alpaca brackets can't express:
    * trailing stop that arms at +0.2% and trails by 0.1%
    * 3-minute time-based exit
    * breakeven tightening when the daily profit lock triggers
  These are enforced by replacing the bracket's stop leg / closing the position.
- Order-rate throttle to stay under Alpaca's request limit.
- Position re-sync from the API on restart.
"""
import asyncio
import logging
import time as time_mod
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, TradeEvent
from alpaca.trading.requests import (
    MarketOrderRequest, TakeProfitRequest, StopLossRequest,
    ReplaceOrderRequest, GetOrdersRequest,
)

from .signals import Signal, Side
from .logger import TradeLogger

log = logging.getLogger("exec")


class RateLimiter:
    """Simple sliding-window limiter for API order calls."""

    def __init__(self, max_per_min: int):
        self.max = max_per_min
        self.stamps: list[float] = []

    async def acquire(self):
        now = time_mod.monotonic()
        self.stamps = [t for t in self.stamps if now - t < 60]
        if len(self.stamps) >= self.max:
            wait = 60 - (now - self.stamps[0]) + 0.05
            log.warning("Order rate limit reached; sleeping %.1fs", wait)
            await asyncio.sleep(wait)
        self.stamps.append(time_mod.monotonic())


@dataclass
class OpenPosition:
    symbol: str
    side: Side
    qty: int
    entry: float
    stop: float
    target: float
    atr_at_entry: float  # ATR captured at entry for trailing stop calculations
    opened_at: datetime
    signal_price: float
    trail_armed: bool = False
    trail_stop: float | None = None
    stop_order_id: str | None = None
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class ExecutionEngine:
    def __init__(self, trading: TradingClient, cfg: dict,
                 tlog: TradeLogger, dry_run: bool):
        self.trading = trading
        self.tlog = tlog
        self.dry_run = dry_run
        e = cfg["exits"]
        # ATR-adaptive targets and stops
        self.atr_tp_mult = e.get("atr_tp_mult", 2.0)
        self.atr_sl_mult = e.get("atr_sl_mult", 1.0)
        # Fallback to fixed percentages if ATR multiples not provided
        self.tp_pct = e.get("profit_target_pct", 0.004)
        self.sl_pct = e.get("hard_stop_pct", 0.0025)
        self.trail_activate = e["trail_activate_pct"]
        self.trail_by = e["trail_by_pct"]
        self.max_hold = e["max_hold_seconds"]
        self.limiter = RateLimiter(cfg["execution"]["order_rate_limit_per_min"])
        self.positions: dict[str, OpenPosition] = {}
        # symbol -> (OpenPosition, reason) for closes awaiting broker fill
        # confirmation via on_trade_update, so logged P&L reflects the real
        # fill price instead of the price at the moment we decided to exit.
        self._pending_exits: dict[str, tuple] = {}

    # ---- entries -----------------------------------------------------------
    async def enter(self, sig: Signal, qty: int, atr: float):
        px = sig.price
        # Use ATR multiples if available, else fall back to percentage-based
        if self.atr_tp_mult > 0 and self.atr_sl_mult > 0:
            if sig.side == Side.LONG:
                stop = px - atr * self.atr_sl_mult
                target = px + atr * self.atr_tp_mult
                order_side = OrderSide.BUY
            else:
                stop = px + atr * self.atr_sl_mult
                target = px - atr * self.atr_tp_mult
                order_side = OrderSide.SELL
        else:
            # Fallback (shouldn't happen in normal operation)
            if sig.side == Side.LONG:
                stop, target = px * (1 - self.sl_pct), px * (1 + self.tp_pct)
                order_side = OrderSide.BUY
            else:
                stop, target = px * (1 + self.sl_pct), px * (1 - self.tp_pct)
                order_side = OrderSide.SELL

        pos = OpenPosition(sig.symbol, sig.side, qty, px, stop, target, atr,
                           datetime.now(timezone.utc), px)

        if self.dry_run:
            self.tlog.order(sig.symbol, sig.side.value, qty, "bracket(DRY)",
                            pos.client_id, "dry_run")
            self.positions[sig.symbol] = pos
            return

        await self.limiter.acquire()
        req = MarketOrderRequest(
            symbol=sig.symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            client_order_id=pos.client_id,
        )
        try:
            order = await asyncio.to_thread(self.trading.submit_order, req)
            self.tlog.order(sig.symbol, sig.side.value, qty, "bracket",
                            pos.client_id, str(order.status))
            self.positions[sig.symbol] = pos
        except Exception:
            log.exception("Order submit failed for %s", sig.symbol)

    # ---- per-bar maintenance ------------------------------------------------
    async def on_price(self, symbol: str, price: float):
        """Trailing stop + time exit management, called on each new bar/quote."""
        pos = self.positions.get(symbol)
        if not pos:
            return

        # time-based exit
        held = (datetime.now(timezone.utc) - pos.opened_at).total_seconds()
        if held >= self.max_hold:
            await self.close_position(symbol, price, "time_exit")
            return

        # trailing stop
        if pos.side == Side.LONG:
            gain = (price - pos.entry) / pos.entry
            if not pos.trail_armed and gain >= self.trail_activate:
                pos.trail_armed = True
                pos.trail_stop = price * (1 - self.trail_by)
            elif pos.trail_armed:
                pos.trail_stop = max(pos.trail_stop, price * (1 - self.trail_by))
                if price <= pos.trail_stop:
                    await self.close_position(symbol, price, "trail_stop")
        else:
            gain = (pos.entry - price) / pos.entry
            if not pos.trail_armed and gain >= self.trail_activate:
                pos.trail_armed = True
                pos.trail_stop = price * (1 + self.trail_by)
            elif pos.trail_armed:
                pos.trail_stop = min(pos.trail_stop, price * (1 + self.trail_by))
                if price >= pos.trail_stop:
                    await self.close_position(symbol, price, "trail_stop")

    # ---- exits ---------------------------------------------------------------
    async def close_position(self, symbol: str, ref_price: float, reason: str):
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        if self.dry_run:
            self.tlog.closed_trade(pos.opened_at.isoformat(), symbol, pos.side.value,
                                   pos.qty, pos.entry, ref_price, reason)
            return
        await self.limiter.acquire()
        try:
            # cancel bracket children then flatten; actual fill price/pnl is
            # logged once on_trade_update confirms the fill.
            await asyncio.to_thread(self.trading.close_position, symbol)
            self._pending_exits[symbol] = (pos, reason)
        except Exception:
            log.exception("close_position failed for %s", symbol)
            self.positions[symbol] = pos  # broker never closed it — keep tracking

    def has_open_or_pending(self, symbol: str) -> bool:
        """True if `symbol` has a tracked open position, or a close that was
        requested but not yet confirmed by the trade-updates stream. Used to
        block re-entry until the prior close is reconciled — otherwise a new
        position could be opened while the old one's fill is still in flight,
        and _pending_exits (one slot per symbol) would lose the earlier trade."""
        return symbol in self.positions or symbol in self._pending_exits

    async def flatten_all(self, reason: str = "eod_flatten"):
        for symbol in list(self.positions):
            pos = self.positions[symbol]
            await self.close_position(symbol, pos.entry, reason)
        if not self.dry_run:
            try:
                await asyncio.to_thread(self.trading.cancel_orders)
            except Exception:
                log.exception("cancel_orders failed")

    async def tighten_to_breakeven(self):
        """Profit-lock: move every open stop to entry."""
        for pos in self.positions.values():
            pos.stop = pos.entry
            pos.trail_armed = True
            pos.trail_stop = pos.entry
            log.info("Stop tightened to breakeven for %s", pos.symbol)

    # ---- broker-side fill reconciliation ---------------------------------
    async def on_trade_update(self, update):
        """Handle Alpaca trade_updates events. Bracket TP/SL legs fill
        server-side without on_price() ever calling close_position() —
        without this, self.positions would keep a phantom entry forever
        (blocking re-entry) and that trade would never be logged."""
        if update.event != TradeEvent.FILL:
            return
        symbol = update.order.symbol
        if float(update.position_qty or 0) != 0:
            return  # entry fill or partial fill, not a close
        exit_px = float(update.price) if update.price is not None else None

        pending = self._pending_exits.pop(symbol, None)
        if pending:
            pos, reason = pending  # bot-initiated close (trail/time/flatten/halt)
        else:
            pos = self.positions.pop(symbol, None)
            if not pos:
                return
            order_type = update.order.type.value if update.order.type else ""
            reason = {"limit": "target", "stop": "stop",
                     "stop_limit": "stop"}.get(order_type, "bracket_exit")

        if exit_px is None:
            exit_px = pos.entry
        self.tlog.closed_trade(pos.opened_at.isoformat(), symbol, pos.side.value,
                               pos.qty, pos.entry, exit_px, reason)

    # ---- restart recovery -----------------------------------------------------
    def resync(self):
        """Rebuild in-memory positions from the API after a restart."""
        if self.dry_run:
            return
        try:
            for p in self.trading.get_all_positions():
                side = Side.LONG if p.side == "long" else Side.SHORT
                entry = float(p.avg_entry_price)
                # No bar history survives a restart, so ATR isn't available here;
                # fall back to percentage-based stop/target like the pre-ATR mode.
                stop = entry * (1 - self.sl_pct) if side == Side.LONG \
                    else entry * (1 + self.sl_pct)
                target = entry * (1 + self.tp_pct) if side == Side.LONG \
                    else entry * (1 - self.tp_pct)
                self.positions[p.symbol] = OpenPosition(
                    p.symbol, side, int(float(p.qty)), entry, stop, target,
                    0.0, datetime.now(timezone.utc), entry)
                log.info("Re-synced position: %s %s x%s @ %.2f",
                         side.value, p.symbol, p.qty, entry)
        except Exception:
            log.exception("Position re-sync failed")
