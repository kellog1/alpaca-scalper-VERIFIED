"""Offline checks for reconcile tolerance and stop planning: python test_order_router.py"""
from order_router import reconcile_positions, plan_stop_actions

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
print("all ok")
