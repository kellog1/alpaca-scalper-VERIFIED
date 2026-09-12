"""Position sizing for the Webull swing agent - cash-account aware.

No leverage: size off *settled* cash only. Webull's own `settled_cash`
balance field already accounts for T+1 settlement, so sizing off it avoids
proposing a trade that would trigger a cash-account good-faith violation.
"""
from dataclasses import dataclass
from typing import Optional

from webull_swing.config import RISK


@dataclass
class PositionSize:
    qty: float
    cost: float
    risk_dollars: float


def size_position(settled_cash: float, open_position_count: int, price: float,
                   stop: float, params: dict = RISK) -> Optional[PositionSize]:
    if open_position_count >= params["max_concurrent_positions"]:
        return None
    stop_distance = price - stop
    if stop_distance <= 0 or settled_cash <= 0 or price <= 0:
        return None

    risk_dollars = settled_cash * params["risk_per_trade_pct"]
    qty = risk_dollars / stop_distance

    # Cap by an equal share of remaining settled cash across open slots, so
    # one tight-stop signal can't eat the whole account in a single trade.
    remaining_slots = params["max_concurrent_positions"] - open_position_count
    max_cost = settled_cash / remaining_slots
    if qty * price > max_cost:
        qty = max_cost / price

    if qty <= 0:
        return None
    cost = qty * price
    return PositionSize(qty=qty, cost=cost, risk_dollars=min(risk_dollars, qty * stop_distance))
