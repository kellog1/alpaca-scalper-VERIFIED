"""
Phase 5: Order Router
-----------------------
Turns target positions (from a signal + risk_manager.py sizing/stops) into
actual Alpaca paper-account orders, and reconciles the account's current
positions against those targets.

IMPORTANT — connectivity constraint (discovered and confirmed in the
originating Claude session): the Claude cloud sandbox's direct network
egress to paper-api.alpaca.markets is blocked by org policy. The only
working path found there was routing HTTP calls through the user's own
browser. That constraint does NOT apply here: alpaca_client.py in this
same folder makes real HTTP calls directly, since it's meant to run on
your own machine / your own CI runner, not in that sandbox.

This module stays deliberately network-free (pure functions that decide
WHAT orders to place) so it's easy to test and reason about independent of
how/where the actual HTTP call happens.
"""
from __future__ import annotations
from dataclasses import dataclass
import json

import pandas as pd


# ---------------------------------------------------------------------------
# Order payload construction (pure — no network calls)
# ---------------------------------------------------------------------------

def build_order_payload(
    symbol: str,
    qty: int,
    side: str,
    stop_price: float | None = None,
    order_type: str = "market",
    time_in_force: str = "day",
) -> dict:
    """
    Builds a payload matching Alpaca's POST /v2/orders schema.
    If stop_price is given, uses order_class='oto' (one-triggers-other) with a
    stop-loss leg attached (Alpaca's 'bracket' requires a take-profit leg too),
    and forces GTC so the stop child does not expire at the close — so every entry this router places carries its risk_manager
    stop with it, rather than relying on a separate manual stop order.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        raise ValueError("qty must be positive")

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }

    if stop_price is not None:
        payload["order_class"] = "oto"
        payload["time_in_force"] = "gtc"
        payload["stop_loss"] = {"stop_price": str(round(stop_price, 2))}

    return payload


@dataclass
class OrderIntent:
    symbol: str
    action: str          # "open_long", "add", "reduce", "close"
    qty: int
    side: str             # "buy" / "sell"
    stop_price: float | None
    reason: str


REBALANCE_TOLERANCE = 0.20   # don't resize a held position unless it is >20% off target


def reconcile_positions(
    current_positions: dict,     # symbol -> current qty (0 if none)
    target_positions: dict,      # symbol -> {"qty": int, "stop_price": float|None}
    rebalance_tolerance: float = REBALANCE_TOLERANCE,
) -> list[OrderIntent]:
    """
    Diffs current vs. target and returns the minimal set of orders needed
    to get there. Opens and closes always happen; resizing an already-held
    position only happens when it is more than `rebalance_tolerance` off
    target, so daily price/ATR drift doesn't trigger constant market orders. Pure function — takes plain dicts, no API calls, so it
    can be unit-tested and reasoned about before anything touches a live
    (even paper) account.
    """
    intents: list[OrderIntent] = []
    all_symbols = set(current_positions) | set(target_positions)

    for symbol in sorted(all_symbols):
        current_qty = current_positions.get(symbol, 0)
        target = target_positions.get(symbol)
        target_qty = target["qty"] if target else 0
        stop_price = target["stop_price"] if target else None

        delta = target_qty - current_qty
        if delta == 0:
            continue

        if current_qty > 0 and target_qty > 0 and abs(delta) / target_qty <= rebalance_tolerance:
            continue

        if current_qty == 0 and target_qty > 0:
            intents.append(OrderIntent(symbol, "open_long", delta, "buy", stop_price, "new target position"))
        elif current_qty > 0 and target_qty == 0:
            intents.append(OrderIntent(symbol, "close", current_qty, "sell", None, "target dropped to zero"))
        elif delta > 0:
            intents.append(OrderIntent(symbol, "add", delta, "buy", stop_price, "increasing toward target"))
        elif delta < 0:
            intents.append(OrderIntent(symbol, "reduce", abs(delta), "sell", None, "decreasing toward target"))

    return intents


def unmanaged_symbols(detail: pd.DataFrame, expected=None) -> set[str]:
    """
    Symbols build_targets could not produce a trustworthy target for: skipped
    rows (NaN features, ATR unavailable, no price data), rows rejected by the
    portfolio constraints, and any `expected` symbol missing from the detail.

    A missing target must mean "don't touch", never "target is zero". Without
    this, reconcile_positions would market-sell a held position, and
    plan_stop_actions would cancel its stop, over a transient data glitch.
    """
    seen = set(detail["symbol"]) if "symbol" in detail else set()
    out: set[str] = set(expected or ()) - seen
    if len(detail) and "symbol" in detail:
        bad = pd.Series(False, index=detail.index)
        if "skipped" in detail:
            bad |= detail["skipped"].notna() & (detail["skipped"].astype(str) != "")
        if "allowed" in detail:
            bad |= detail["allowed"] == False   # noqa: E712 (NaN on skipped rows must not count twice)
        out |= set(detail.loc[bad, "symbol"])
    return out


def hold_unmanaged(current_positions: dict, unmanaged) -> dict:
    """Positions the reconciler and stop planner may act on: everything except
    the unmanaged symbols, which are left exactly as they are (position and stop)."""
    return {s: q for s, q in current_positions.items() if s not in unmanaged}


def symbols_with_pending_orders(open_orders: list[dict]) -> set[str]:
    """
    Symbols with an open order other than a plain protective sell-stop: an
    unfilled entry, a closing/reducing sell, a limit order, etc.

    The workflow fires several times a day, so a symbol with work still in
    flight must be left alone; reconciling it again would submit a duplicate
    order. Protective stops are excluded because they are expected to stay open
    (often overnight) and are managed by plan_stop_actions.
    """
    return {o["symbol"] for o in open_orders
            if o.get("symbol") and not (o.get("type") == "stop" and o.get("side") == "sell")}


MIN_MINUTES_TO_CLOSE = 15   # don't send orders in the last 15 minutes of the session


def market_open_check(clock: dict, min_minutes_to_close: float = MIN_MINUTES_TO_CLOSE) -> tuple[bool, str]:
    """Pure guard on an Alpaca /v2/clock payload: (ok_to_trade, reason).

    Uses the clock's own timestamp so the decision doesn't depend on the local
    machine's time. Anything malformed or missing fails closed.
    """
    try:
        if not clock.get("is_open"):
            return False, f"market closed (next open {clock.get('next_open')})"
        now = pd.Timestamp(clock["timestamp"])
        close = pd.Timestamp(clock["next_close"])
        mins = (close - now).total_seconds() / 60
    except Exception as e:  # missing/garbled fields: fail closed
        return False, f"could not read market clock ({type(e).__name__}: {e})"
    if mins < min_minutes_to_close:
        return False, f"only {mins:.0f} min to close (< {min_minutes_to_close:g})"
    return True, f"market open, {mins:.0f} min to close"


STOP_RATCHET_MIN = 0.005   # only raise an existing stop if the new one is >0.5% higher


def build_stop_payload(symbol: str, qty: int, stop_price: float) -> dict:
    """Standalone protective sell-stop (GTC) for shares already held."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    return {
        "symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "stop",
        "stop_price": str(round(stop_price, 2)), "time_in_force": "gtc",
    }


def plan_stop_actions(
    current_positions: dict,     # symbol -> signed qty (basket only)
    target_positions: dict,      # symbol -> {"qty": int, "stop_price": float}
    open_orders: list[dict],     # Alpaca open orders (raw dicts)
) -> list[dict]:
    """
    Keeps every held long protected by exactly one stop that matches the
    position size (or the target size when reducing), and ratchets it up
    with the ATR stop but never lowers it. Pure function; all actions apply
    to shares already held, so they can run before any market orders.

    Actions: {"action": "place"|"replace"|"cancel", "symbol", ...}
      place   -> payload
      replace -> order_id, qty, stop_price   (PATCH an existing order)
      cancel  -> order_id
    Symbols with no target (being closed) get their stops cancelled so the
    closing sell isn't blocked by shares held for a stop.
    """
    actions: list[dict] = []
    for sym, qty in sorted(current_positions.items()):
        if qty <= 0:
            continue
        stops = [o for o in open_orders
                 if o.get("symbol") == sym and o.get("type") == "stop" and o.get("side") == "sell"]
        target = target_positions.get(sym)

        if target is None:
            for o in stops:
                actions.append({"action": "cancel", "symbol": sym, "order_id": o["id"],
                                "reason": "position being closed"})
            continue

        want_qty = int(min(qty, target["qty"]))
        new_stop = round(float(target["stop_price"]), 2)
        cur_stop = max((float(o["stop_price"]) for o in stops), default=None)
        stop_price = new_stop if cur_stop is None else max(new_stop, cur_stop)  # never lower

        if not stops:
            actions.append({"action": "place", "symbol": sym,
                            "payload": build_stop_payload(sym, want_qty, stop_price),
                            "reason": "held position has no stop"})
        elif len(stops) == 1:
            o = stops[0]
            qty_ok = int(float(o["qty"])) == want_qty
            raise_ok = new_stop > float(o["stop_price"]) * (1 + STOP_RATCHET_MIN)
            if not qty_ok or raise_ok:
                actions.append({"action": "replace", "symbol": sym, "order_id": o["id"],
                                "qty": want_qty, "stop_price": stop_price,
                                "reason": "resize stop to position" if not qty_ok else "ratchet stop up"})
        else:
            for o in stops:
                actions.append({"action": "cancel", "symbol": sym, "order_id": o["id"],
                                "reason": "consolidating multiple stops"})
            actions.append({"action": "place", "symbol": sym,
                            "payload": build_stop_payload(sym, want_qty, stop_price),
                            "reason": "single consolidated stop"})
    return actions


def intents_to_payloads(intents: list[OrderIntent]) -> list[dict]:
    return [
        build_order_payload(i.symbol, i.qty, i.side, stop_price=i.stop_price)
        for i in intents
    ]


# ---------------------------------------------------------------------------
# Browser-fetch JS snippets — kept for reference / for use back in a Claude
# session with browser tools. Not used by run_daily.py in this folder,
# which calls alpaca_client.py's real HTTP functions directly instead.
# ---------------------------------------------------------------------------

ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"


def get_positions_js_snippet(api_key: str, api_secret: str) -> str:
    return f"""
fetch('{ALPACA_BASE_URL}/positions', {{
  headers: {{
    'APCA-API-KEY-ID': '{api_key}',
    'APCA-API-SECRET-KEY': '{api_secret}'
  }}
}}).then(r => r.json()).then(positions =>
  Object.fromEntries(positions.map(p => [p.symbol, parseFloat(p.qty)]))
)
""".strip()


def place_orders_js_snippet(payloads: list[dict], api_key: str, api_secret: str) -> str:
    payloads_json = json.dumps(payloads)
    return f"""
(async () => {{
  const payloads = {payloads_json};
  const results = [];
  for (const payload of payloads) {{
    try {{
      const resp = await fetch('{ALPACA_BASE_URL}/orders', {{
        method: 'POST',
        headers: {{
          'APCA-API-KEY-ID': '{api_key}',
          'APCA-API-SECRET-KEY': '{api_secret}',
          'Content-Type': 'application/json'
        }},
        body: JSON.stringify(payload)
      }});
      const body = await resp.json();
      results.push({{symbol: payload.symbol, status: resp.status, body}});
    }} catch (e) {{
      results.push({{symbol: payload.symbol, error: String(e)}});
    }}
  }}
  return results;
}})()
""".strip()
