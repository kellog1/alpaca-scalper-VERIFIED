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
