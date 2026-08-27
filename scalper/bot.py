"""Bot orchestrator: wires stream -> signals -> risk -> execution.

Async design: the WebSocket handler dispatches bars for all symbols
concurrently; a supervisor loop enforces daily limits, session
transitions, EOD flatten, and the profit lock.
"""
import asyncio
import logging
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from .config import Config, Credentials
from .datastream import DataStream
from .execution import ExecutionEngine
from .logger import TradeLogger
from .risk import RiskManager
from .signals import SignalEngine, Side

log = logging.getLogger("bot")


class ScalperBot:
    def __init__(self, cfg: Config, creds: Credentials):
        self.cfg = cfg.raw
        self.dry_run = self.cfg["execution"]["dry_run"]
        self.trading = TradingClient(creds.api_key, creds.secret_key, paper=True)
        self.tlog = TradeLogger(self.cfg["logging"]["sqlite_path"])
        self.signals = SignalEngine(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.exec = ExecutionEngine(self.trading, self.cfg, self.tlog, self.dry_run)
        self.stream = DataStream(
            creds.api_key, creds.secret_key,
            self.cfg["symbols"], self.cfg["bars"]["timeframe_seconds"],
            on_bar=self.on_bar, on_tick=self.exec.on_price,
        )
        self._flattened_today = False
        self._eod_reported = False
        self._current_day = None

        # Reconciles broker-side bracket TP/SL fills, which the price-tick
        # loop never sees (see ExecutionEngine.on_trade_update).
        self.trading_stream = None if self.dry_run else TradingStream(
            creds.api_key, creds.secret_key, paper=True)
        if self.trading_stream:
            self.trading_stream.subscribe_trade_updates(self.exec.on_trade_update)

    # ---- account helpers ---------------------------------------------------
    async def _account(self):
        # TradingClient is a blocking REST client; run it off the event loop
        # so a slow API call doesn't stall bar/tick processing for every symbol.
        return await asyncio.to_thread(self.trading.get_account)

    def _open_position_value(self) -> float:
        return sum(p.qty * p.entry for p in self.exec.positions.values())

    # ---- daily lifecycle ---------------------------------------------------
    async def _check_day_rollover(self):
        """Re-arm daily state (loss halt, profit lock, EOD flatten/report) on
        a new trading day. Without this, a bot left running past midnight
        would keep using day-1's starting equity forever, never flatten at
        close again, and never print another EOD report."""
        day = datetime.now(self.risk.tz).date()
        if day == self._current_day:
            return
        self._current_day = day
        acct = await self._account()
        self.risk.start_of_day(float(acct.equity))
        self._flattened_today = False
        self._eod_reported = False
        log.info("New trading day: %s", day)

    # ---- bar handler ---------------------------------------------------------
    async def on_bar(self, symbol: str, bar: dict):
        await self._check_day_rollover()
        session = self.risk.session_state()

        if session == "flatten" and not self._flattened_today:
            log.info("Flatten window reached — closing everything.")
            await self.exec.flatten_all()
            self._flattened_today = True
            return
        if session in ("flatten", "closed"):
            return

        sig = self.signals.on_bar(symbol, bar)
        if sig is None:
            return

        # one position per symbol at a time — also blocks re-entry while a
        # prior close on this symbol is still awaiting broker confirmation,
        # since that fill hasn't been reconciled into the trade log yet.
        if self.exec.has_open_or_pending(symbol):
            self.tlog.signal(symbol, sig.side.value, sig.price, sig.reason, acted=False)
            return
        if not self.risk.can_open(len(self.exec.positions), session):
            self.tlog.signal(symbol, sig.side.value, sig.price, sig.reason, acted=False)
            return

        acct = await self._account()
        equity = float(acct.equity)
        buying_power = float(acct.buying_power)
        
        # Compute stop for position sizing
        atr = sig.atr
        if self.cfg["exits"].get("atr_sl_mult", 0) > 0:
            # ATR-based stop
            stop = sig.price - atr * self.cfg["exits"]["atr_sl_mult"] if sig.side == Side.LONG \
                   else sig.price + atr * self.cfg["exits"]["atr_sl_mult"]
        else:
            # Percentage-based stop (fallback)
            stop = sig.price * (1 - self.cfg["exits"]["hard_stop_pct"]) \
                if sig.side == Side.LONG else \
                sig.price * (1 + self.cfg["exits"]["hard_stop_pct"])
        
        qty = self.risk.position_size(
            equity, buying_power, sig.price, stop,
            self._open_position_value(), session)

        acted = qty > 0
        self.tlog.signal(symbol, sig.side.value, sig.price, sig.reason, acted)
        if acted:
            await self.exec.enter(sig, qty, atr)  # pass ATR for TP/SL computation

    # ---- supervisor loop --------------------------------------------------------
    async def supervisor(self):
        """Every 15s: daily limits, profit lock, halt-flatten, EOD report."""
        while True:
            try:
                await self._check_day_rollover()
                acct = await self._account()
                state = self.risk.check_daily_limits(float(acct.equity))
                if state["halt"] and self.exec.positions:
                    await self.exec.flatten_all("daily_loss_halt")
                if state["lock_profits"]:
                    await self.exec.tighten_to_breakeven()

                session = self.risk.session_state()
                if session == "closed" and self._flattened_today and not self._eod_reported:
                    self.tlog.eod_report()
                    self._eod_reported = True
            except Exception:
                log.exception("Supervisor iteration failed")
            await asyncio.sleep(15)

    # ---- entrypoint -----------------------------------------------------------------
    async def run(self):
        acct = await self._account()
        self._current_day = datetime.now(self.risk.tz).date()
        self.risk.start_of_day(float(acct.equity))
        self.exec.resync()
        mode = "DRY RUN" if self.dry_run else "PAPER TRADING"
        log.info("Starting scalper in %s mode. Equity: $%s", mode, acct.equity)
        tasks = [self.stream.run_forever(), self.supervisor()]
        if self.trading_stream:
            tasks.append(self.trading_stream._run_forever())
        await asyncio.gather(*tasks)
