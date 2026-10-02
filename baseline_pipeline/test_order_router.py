"""Offline checks for reconcile tolerance and stop planning: python test_order_router.py"""
from order_router import reconcile_positions, plan_stop_actions, market_open_check

T = lambda q, s: {"qty": q, "stop_price": s}
stop = lambda i, sym, q, p: {"id": i, "symbol": sym, "type": "stop", "side": "sell", "qty": str(q), "stop_price": str(p)}

# --- drift tolerance ---
assert reconcile_positions({"SPY": 100}, {"SPY": T(110, 1)}) == []            # 10% off: hold
assert reconcile_positions({"SPY": 100}, {"SPY": T(130, 1)})[0].action == "add"   # 30% off
assert reconcile_positions({"SPY": 100}, {"SPY": T(60, 1)})[0].action == "reduce"
assert reconcile_positions({}, {"SPY": T(5, 1)})[0].action == "open_long"     # opens always happen
assert reconcile_positions({"SPY": 100}, {})[0].action == "close"             # closes always happen

# --- stops ---
pos = {"SPY": 100}
a = plan_stop_actions(pos, {"SPY": T(100, 95)}, [])
assert [x["action"] for x in a] == ["place"] and a[0]["payload"]["qty"] == "100"
assert plan_stop_actions(pos, {"SPY": T(100, 95)}, [stop("1", "SPY", 100, 95)]) == []        # in sync
assert plan_stop_actions(pos, {"SPY": T(100, 95.2)}, [stop("1", "SPY", 100, 95)]) == []      # <0.5% raise
a = plan_stop_actions(pos, {"SPY": T(100, 97)}, [stop("1", "SPY", 100, 95)])
assert a[0]["action"] == "replace" and a[0]["stop_price"] == 97                                # ratchet up
a = plan_stop_actions(pos, {"SPY": T(100, 90)}, [stop("1", "SPY", 80, 95)])
assert a[0]["action"] == "replace" and a[0]["stop_price"] == 95 and a[0]["qty"] == 100         # never lower
a = plan_stop_actions(pos, {"SPY": T(60, 95)}, [stop("1", "SPY", 100, 95)])
assert a[0]["action"] == "replace" and a[0]["qty"] == 60                                       # shrink before reduce
a = plan_stop_actions(pos, {}, [stop("1", "SPY", 100, 95)])
assert [x["action"] for x in a] == ["cancel"]                                                  # closing
a = plan_stop_actions(pos, {"SPY": T(100, 96)}, [stop("1", "SPY", 60, 95), stop("2", "SPY", 40, 94)])
assert [x["action"] for x in a] == ["cancel", "cancel", "place"] and a[2]["payload"]["stop_price"] == "96.0"
assert plan_stop_actions({"SPY": -5}, {"SPY": T(1, 1)}, []) == []                              # shorts untouched
assert plan_stop_actions(pos, {"SPY": T(100, 95)}, [stop("9", "AAPL", 10, 1)])[0]["action"] == "place"  # other symbols ignored
# --- market guard ---
clk = lambda **k: {"is_open": True, "timestamp": "2026-10-02T10:30:00-04:00", "next_close": "2026-10-02T16:00:00-04:00",
                   "next_open": "2026-10-05T09:30:00-04:00", **k}
ok, why = market_open_check(clk()); assert ok and "330 min" in why, why
assert market_open_check(clk(is_open=False))[0] is False
assert market_open_check(clk(timestamp="2026-10-02T15:50:00-04:00"))[0] is False        # 10 min left
assert market_open_check(clk(timestamp="2026-10-02T15:40:00-04:00"))[0] is True          # 20 min left
assert market_open_check(clk(timestamp="2026-10-02T15:45:00-04:00"))[0] is True          # exactly 15
assert market_open_check({})[0] is False and market_open_check({"is_open": True})[0] is False   # malformed: fail closed
assert market_open_check(clk(next_close="garbage"))[0] is False
print("all ok")
