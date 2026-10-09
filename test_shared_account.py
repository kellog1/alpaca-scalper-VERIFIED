"""Offline checks for shared-account behavior: python test_shared_account.py"""
import asyncio, sys, types
sys.path.insert(0, ".")
from scalper.config import load_config, tradable_symbols
from scalper.execution import ExecutionEngine, CLIENT_ID_PREFIX, OpenPosition
from scalper.signals import Side
from datetime import datetime, timezone

cfg = load_config("config.yaml").raw
assert tradable_symbols(cfg) == ["TSLA","AMD","MARA","SOXL","SOXS"], tradable_symbols(cfg)

class FakeTrading:
    def __init__(s):
        s.cancelled=[]; s.req=None
        s.pos=[types.SimpleNamespace(symbol=x,side="long",avg_entry_price="10",qty="5") for x in ("TSLA","SPY","WBD","NVDA")]
    def get_all_positions(s): return s.pos
    def get_orders(s, req): s.req=req; return [types.SimpleNamespace(id="o1"), types.SimpleNamespace(id="o2")]
    def cancel_order_by_id(s, i): s.cancelled.append(i)
    def cancel_orders(s): raise AssertionError("blanket cancel_orders must not be called")
    def close_position(s, sym): pass

ft = FakeTrading()
e = ExecutionEngine(ft, cfg, types.SimpleNamespace(closed_trade=lambda *a, **k: None), dry_run=False)
e.resync()
assert list(e.positions) == ["TSLA"], list(e.positions)          # only scalper symbols adopted
assert OpenPosition("X", Side.LONG, 1, 1, 1, 1, 0, datetime.now(timezone.utc), 1).client_id.startswith(CLIENT_ID_PREFIX)
asyncio.run(e.flatten_all())
assert sorted(ft.req.symbols) == ["AMD","MARA","SOXL","SOXS","TSLA"]   # scoped cancel, no reserved symbols
assert ft.cancelled == ["o1","o2"]
print("scalper ownership checks ok")

# --- halt / profit lock use the scalper's own P&L, not account equity ---
import copy
from scalper.config import Config, Credentials
from scalper.bot import ScalperBot
from scalper.risk import RiskManager
from scalper.logger import TradeLogger

c = copy.deepcopy(cfg)
c["execution"]["dry_run"] = True
c["logging"]["sqlite_path"] = ":memory:"
bot = ScalperBot(Config(raw=c), Credentials("k", "s"))
bot.risk.start_of_day(100_000.0)
bot._mark_day_start(datetime.now(bot.risk.tz).date())

# pipeline swings account equity +/-10%: scalper P&L is still zero -> no halt, no lock
assert bot._strategy_pnl() == 0.0
st = bot.risk.check_daily_pnl(bot._strategy_pnl())
assert st == {"halt": False, "lock_profits": False}, st

# own realized loss beyond -6% halts
bot.tlog.closed_trade(datetime.now(timezone.utc).isoformat(), "TSLA", "long", 100, 300.0, 240.0, "test")  # -6000
assert bot._strategy_pnl() == -6000.0
assert bot.risk.check_daily_pnl(bot._strategy_pnl())["halt"] is True

# a trade closed yesterday does not count today
bot.tlog.conn.execute("INSERT INTO trades VALUES ('2000-01-01T00:00:00+00:00','2000-01-01T00:01:00+00:00','X','long',1,1,1,-99999,60,'old')")
assert bot._strategy_pnl() == -6000.0

# unrealized on own positions counts (long +$500, short +$100); foreign/reserved never enter self.positions
r2 = RiskManager(c); r2.start_of_day(100_000.0)
bot2 = ScalperBot(Config(raw=c), Credentials("k", "s"))
now = datetime.now(timezone.utc)
bot2.exec.positions["TSLA"] = OpenPosition("TSLA", Side.LONG, 100, 100.0, 90, 110, 1, now, 100.0)
bot2.exec.positions["AMD"] = OpenPosition("AMD", Side.SHORT, 50, 100.0, 110, 90, 1, now, 100.0)
bot2.exec.last_price.update({"TSLA": 105.0, "AMD": 98.0})
assert bot2.exec.unrealized_pnl() == 600.0
assert r2.check_daily_pnl(5_000.0)["lock_profits"] is True           # +5% of own P&L locks profit
print("shared-account P&L checks ok")
