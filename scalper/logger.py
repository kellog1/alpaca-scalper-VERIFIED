"""Trade logger: console + SQLite persistence and end-of-day report.

Tables:
  signals(ts, symbol, side, price, reason, acted)
  orders(ts, symbol, side, qty, order_type, client_id, status)
  fills(ts, symbol, side, qty, price, order_id, slippage_est)
  trades(open_ts, close_ts, symbol, side, qty, entry, exit, pnl, hold_secs, exit_reason)
"""
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("scalper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  ts TEXT, symbol TEXT, side TEXT, price REAL, reason TEXT, acted INTEGER);
CREATE TABLE IF NOT EXISTS orders(
  ts TEXT, symbol TEXT, side TEXT, qty INTEGER, order_type TEXT,
  client_id TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS fills(
  ts TEXT, symbol TEXT, side TEXT, qty INTEGER, price REAL,
  order_id TEXT, slippage_est REAL);
CREATE TABLE IF NOT EXISTS trades(
  open_ts TEXT, close_ts TEXT, symbol TEXT, side TEXT, qty INTEGER,
  entry REAL, exit REAL, pnl REAL, hold_secs REAL, exit_reason TEXT);
"""


def setup_console_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class TradeLogger:
    def __init__(self, sqlite_path: str):
        self.conn = sqlite3.connect(sqlite_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def signal(self, symbol, side, price, reason, acted: bool):
        log.info("SIGNAL %s %s @ %.2f (%s)%s", side, symbol, price, reason,
                 "" if acted else " [not acted]")
        self.conn.execute("INSERT INTO signals VALUES (?,?,?,?,?,?)",
                          (self._now(), symbol, side, price, reason, int(acted)))
        self.conn.commit()

    def order(self, symbol, side, qty, order_type, client_id, status):
        log.info("ORDER  %s %s x%d (%s) -> %s", side, symbol, qty, order_type, status)
        self.conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                          (self._now(), symbol, side, qty, order_type, client_id, status))
        self.conn.commit()

    def fill(self, symbol, side, qty, price, order_id, signal_price: Optional[float]):
        slip = (price - signal_price) if signal_price is not None else None
        log.info("FILL   %s %s x%d @ %.2f (slip %s)", side, symbol, qty, price,
                 f"{slip:+.3f}" if slip is not None else "n/a")
        self.conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                          (self._now(), symbol, side, qty, price, order_id, slip))
        self.conn.commit()

    def closed_trade(self, open_ts, symbol, side, qty, entry, exit_px, exit_reason):
        close_ts = self._now()
        pnl = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
        hold = (datetime.fromisoformat(close_ts)
                - datetime.fromisoformat(open_ts)).total_seconds()
        log.info("CLOSED %s %s x%d entry %.2f exit %.2f pnl %+.2f (%s, %.0fs)",
                 side, symbol, qty, entry, exit_px, pnl, exit_reason, hold)
        self.conn.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (open_ts, close_ts, symbol, side, qty, entry, exit_px,
                           pnl, hold, exit_reason))
        self.conn.commit()

    # ---- end-of-day report ----------------------------------------------
    def eod_report(self) -> str:
        cur = self.conn.execute(
            "SELECT COUNT(*), SUM(pnl), AVG(hold_secs), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END), "
            "SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) "
            "FROM trades WHERE date(close_ts)=date('now')")
        n, pnl, avg_hold, wins, gross_win, gross_loss = cur.fetchone()
        if not n:
            return "EOD: no trades today."
        win_rate = wins / n * 100
        pf = (gross_win / gross_loss) if gross_loss else float("inf")

        # max drawdown over today's cumulative trade P&L
        cur = self.conn.execute(
            "SELECT pnl FROM trades WHERE date(close_ts)=date('now') ORDER BY close_ts")
        cum = peak = 0.0
        max_dd = 0.0
        for (p,) in cur:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        report = (f"EOD REPORT — trades={n} pnl={pnl:+.2f} win_rate={win_rate:.1f}% "
                  f"profit_factor={pf:.2f} avg_hold={avg_hold:.0f}s max_dd={max_dd:.2f}")
        log.info(report)
        return report
