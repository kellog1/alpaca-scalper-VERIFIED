"""Decision logic for the AI-infrastructure swing agent.

No I/O to Webull or any broker lives here - it's pure logic over data the
caller supplies. The actual daily workflow (fetch bars + balance from
Webull, run screen(), present proposals, place an order only once the user
confirms) happens inside a Claude session using the Webull MCP tools - see
README.md.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional

from webull_swing.config import RISK, STRATEGY, WATCHLIST
from webull_swing.risk import size_position
from webull_swing.signals import breakout_signal, check_exit
from webull_swing.state import load_state, save_state


@dataclass
class ExitProposal:
    symbol: str
    reason: str
    entry_price: float
    exit_price: float
    qty: float


@dataclass
class EntryProposal:
    symbol: str
    price: float
    stop: float
    target: float
    qty: float
    cost: float
    reason: str


def screen(bars_by_symbol: dict, settled_cash: float,
           params_strategy: dict = STRATEGY, params_risk: dict = RISK):
    """Given the latest daily bars per symbol and available settled cash,
    return (exit_proposals, entry_proposals)."""
    state = load_state()
    positions = state.get("positions", {})

    exits = []
    for symbol, pos in positions.items():
        bars = bars_by_symbol.get(symbol)
        if not bars:
            continue
        entry_idx = next((i for i, b in enumerate(bars) if b["date"] == pos["entry_date"]), None)
        if entry_idx is None:
            continue
        reason = check_exit(pos["entry_price"], pos["stop"], pos["target"], bars,
                             entry_idx, params_strategy["max_hold_days"])
        if reason:
            exit_price = {"stop": pos["stop"], "target": pos["target"]}.get(reason, bars[-1]["close"])
            exits.append(ExitProposal(symbol, reason, pos["entry_price"], exit_price, pos["qty"]))

    exiting_symbols = {e.symbol for e in exits}
    open_count = len([s for s in positions if s not in exiting_symbols])

    entries = []
    for symbol in WATCHLIST:
        if symbol in positions and symbol not in exiting_symbols:
            continue  # already holding it, not exiting today
        bars = bars_by_symbol.get(symbol)
        if not bars:
            continue
        sig = breakout_signal(symbol, bars, params_strategy)
        if sig is None:
            continue
        size = size_position(settled_cash, open_count, sig.price, sig.stop, params_risk)
        if size is None:
            continue
        entries.append(EntryProposal(symbol, sig.price, sig.stop, sig.target,
                                      round(size.qty, 4), round(size.cost, 2), sig.reason))
        open_count += 1  # reserve a slot so later signals this run size correctly

    return exits, entries


def record_fill(symbol: str, side: str, qty: float, price: float,
                 stop: Optional[float] = None, target: Optional[float] = None,
                 fill_date: Optional[str] = None):
    """Update local position state after a proposed trade is actually
    confirmed and filled in Webull. Call this only once the user confirms
    execution - this function itself never talks to Webull."""
    state = load_state()
    positions = state.setdefault("positions", {})
    fill_date = fill_date or date.today().isoformat()
    if side == "BUY":
        positions[symbol] = {"entry_price": price, "qty": qty, "stop": stop,
                              "target": target, "entry_date": fill_date}
    elif side == "SELL":
        positions.pop(symbol, None)
    save_state(state)
