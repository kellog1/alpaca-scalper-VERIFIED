"""
Phase 4: Risk Manager
----------------------
Position sizing, stop placement, and portfolio-level constraints — the
execution-safety layer that everything else (signals, the vol-expansion
model, eventual order routing) plugs into before any capital is committed.

This directly targets a concrete problem found in the paper account's trade
history: position sizes were wildly inconsistent and apparently unrelated to
risk (MARA got a 41,768-share position vs. AMD's 5-840 shares), with no
visible sizing or stop logic. MARA was also the account's worst historical
performer (-$1,561.99 P&L, 35 trades); AMD was the best (+$853.79, 19 trades).

Design:
  1. position_size()      — fixed-fractional risk sizing: risk a fixed % of
                             equity per trade, sized off the actual stop
                             distance, capped by a max-% of equity and by
                             available buying power.
  2. atr_stop()            — volatility-based stop placement using the ATR
                             function already validated in feature_extractor.py.
  3. PortfolioConstraints   — account-level guardrails: max concurrent
                             positions, max weight per symbol, daily loss
                             kill switch.
  4. vol_adjusted_risk_pct()— optional hook: shrink the per-trade risk
                             budget when the volatility-expansion model
                             (ml_ensemble_vol.py) predicts vol expansion,
                             instead of sizing as if current calm conditions
                             will persist.

No shortcuts here either — this is the layer meant to stand between the
system and real capital, so every function is deliberately conservative
and fails safe (returns 0 shares / rejects the trade) rather than guessing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from feature_extractor import _atr


# ---------------------------------------------------------------------------
# 1. Position sizing
# ---------------------------------------------------------------------------

def position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    risk_pct: float = 0.01,
    max_position_pct: float = 0.10,
    buying_power: float | None = None,
) -> dict:
    """
    Fixed-fractional risk sizing.

    Risk `risk_pct` of equity (default 1%) on this trade, where the "risk"
    is defined by the actual distance to the stop — not by an arbitrary
    dollar amount or share count. Capped by:
      - max_position_pct of equity (so one name, even with a tight stop,
        can't dominate the book)
      - buying_power, if provided (so the sizer never proposes a position
        the account can't actually take)

    Returns a dict rather than a bare int so the caller can see WHY a size
    was reduced or zeroed out — important for something that will eventually
    handle real money.
    """
    if equity <= 0:
        return {"shares": 0, "reason": "non-positive equity"}
    if entry_price <= 0:
        return {"shares": 0, "reason": "non-positive entry price"}

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return {"shares": 0, "reason": "zero stop distance (entry == stop)"}

    risk_dollars = equity * risk_pct
    shares_by_risk = risk_dollars / stop_distance

    max_position_dollars = equity * max_position_pct
    shares_by_max_pct = max_position_dollars / entry_price

    shares = min(shares_by_risk, shares_by_max_pct)

    capped_by = "risk" if shares_by_risk <= shares_by_max_pct else "max_position_pct"

    if buying_power is not None:
        shares_by_bp = buying_power / entry_price
        if shares_by_bp < shares:
            shares = shares_by_bp
            capped_by = "buying_power"

    shares = int(max(shares, 0))
    position_dollars = shares * entry_price
    dollars_at_risk = shares * stop_distance

    return {
        "shares": shares,
        "position_dollars": round(position_dollars, 2),
        "dollars_at_risk": round(dollars_at_risk, 2),
        "pct_equity": round(position_dollars / equity, 4) if equity else None,
        "pct_risk": round(dollars_at_risk / equity, 4) if equity else None,
        "capped_by": capped_by,
    }


# ---------------------------------------------------------------------------
# 2. Stop placement
# ---------------------------------------------------------------------------

def atr_stop(
    df: pd.DataFrame,
    entry_price: float,
    direction: str = "long",
    atr_window: int = 14,
    atr_multiple: float = 2.0,
    as_of_index: int = -1,
) -> dict:
    """
    Volatility-based stop: entry -/+ atr_multiple * ATR(atr_window), using
    the same ATR calculation already used (and implicitly validated) in
    feature_extractor.py's feature panel.

    `as_of_index` lets you place a stop using the ATR as it stood at any
    historical bar (default: most recent bar available in df) — needed for
    the backtest-style demonstration below, where we walk through history
    bar-by-bar rather than only ever looking at "now".
    """
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")

    atr_series = _atr(df, window=atr_window)
    atr_value = atr_series.iloc[as_of_index]

    if pd.isna(atr_value) or atr_value <= 0:
        return {"stop_price": None, "atr": atr_value, "reason": "ATR unavailable (insufficient history)"}

    offset = atr_multiple * atr_value
    stop_price = entry_price - offset if direction == "long" else entry_price + offset

    return {
        "stop_price": round(float(stop_price), 4),
        "atr": round(float(atr_value), 4),
        "atr_multiple": atr_multiple,
        "stop_distance": round(float(offset), 4),
        "stop_distance_pct": round(float(offset / entry_price), 4),
    }


# ---------------------------------------------------------------------------
# 3. Portfolio-level constraints
# ---------------------------------------------------------------------------

@dataclass
class PortfolioConstraints:
    """
    Account-level guardrails. These sit above individual position sizing:
    a trade can pass position_size() and atr_stop() cleanly and still be
    rejected here because of what it would do to the whole book.
    """
    max_positions: int = 8
    max_symbol_weight: float = 0.15       # no single symbol > 15% of equity
    daily_loss_limit_pct: float = 0.03    # kill switch: -3% equity in a day halts new entries

    open_positions: dict = field(default_factory=dict)   # symbol -> position_dollars
    daily_pnl_pct: float = 0.0                            # running total, reset each session day

    def can_open_new_position(self, symbol: str, proposed_dollars: float, equity: float) -> dict:
        if self.daily_pnl_pct <= -abs(self.daily_loss_limit_pct):
            return {"allowed": False, "reason": f"daily loss limit hit ({self.daily_pnl_pct:.2%}) — kill switch engaged"}

        if symbol not in self.open_positions and len(self.open_positions) >= self.max_positions:
            return {"allowed": False, "reason": f"max_positions ({self.max_positions}) already open"}

        existing = self.open_positions.get(symbol, 0.0)
        new_total = existing + proposed_dollars
        weight = new_total / equity if equity else float("inf")
        if weight > self.max_symbol_weight:
            return {
                "allowed": False,
                "reason": f"would push {symbol} to {weight:.1%} of equity, over max_symbol_weight ({self.max_symbol_weight:.1%})",
            }

        return {"allowed": True, "reason": None, "resulting_weight": round(weight, 4)}

    def register_fill(self, symbol: str, position_dollars: float):
        self.open_positions[symbol] = self.open_positions.get(symbol, 0.0) + position_dollars

    def register_close(self, symbol: str):
        self.open_positions.pop(symbol, None)

    def update_daily_pnl(self, pnl_pct_change: float):
        self.daily_pnl_pct += pnl_pct_change

    def reset_daily(self):
        self.daily_pnl_pct = 0.0


# ---------------------------------------------------------------------------
# 4. Optional hook: volatility-model-aware risk sizing
# ---------------------------------------------------------------------------

def vol_adjusted_risk_pct(base_risk_pct: float, vol_expansion_proba: float | None, shrink_factor: float = 0.5) -> float:
    """
    If the volatility-expansion model (ml_ensemble_vol.py) is available and
    predicts elevated probability of a vol expansion over the next horizon,
    shrink the risk budget rather than sizing as though current (lower)
    volatility will hold. This is a hook, not a hard dependency — pass
    vol_expansion_proba=None to size without it.

    shrink_factor of 0.5 at proba=1.0 means: at maximum model confidence in
    an upcoming vol expansion, risk is cut in half. Linear in between.
    """
    if vol_expansion_proba is None:
        return base_risk_pct
    proba = min(max(vol_expansion_proba, 0.0), 1.0)
    multiplier = 1.0 - shrink_factor * proba
    return base_risk_pct * multiplier


# ---------------------------------------------------------------------------
# Demonstration: what disciplined sizing/stops would have changed for
# MARA (worst historical performer) vs AMD (best), using real price history.
# ---------------------------------------------------------------------------

def _load(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(f"data/{symbol}.csv", parse_dates=["date"]).set_index("date")
    return df.sort_index()


def demo(symbol: str, account_equity: float = 100_000, risk_pct: float = 0.01, sample_every: int = 40):
    """
    Walk through a symbol's price history, and at regular intervals, compute
    what an ATR-based stop and fixed-fractional position size WOULD have
    been at that point — purely illustrative (not a backtest of entries/exits,
    since we don't have the actual historical fill-level trade log in this
    session). The point is to show how position size should vary with
    volatility, in contrast to the flat/ad-hoc sizing seen in the actual
    paper account trades (e.g. MARA's 41,768-share position).
    """
    df = _load(symbol)
    rows = []
    for i in range(60, len(df), sample_every):
        window = df.iloc[: i + 1]
        entry_price = float(window["close"].iloc[-1])
        stop = atr_stop(window, entry_price, direction="long")
        if stop["stop_price"] is None:
            continue
        sized = position_size(account_equity, entry_price, stop["stop_price"], risk_pct=risk_pct)
        rows.append({
            "date": window.index[-1].date(),
            "close": round(entry_price, 2),
            "atr_14": stop["atr"],
            "stop_price": stop["stop_price"],
            "stop_dist_pct": stop["stop_distance_pct"],
            "shares": sized["shares"],
            "position_dollars": sized["position_dollars"],
            "capped_by": sized["capped_by"],
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 140)

    print("=" * 78)
    print("RISK MANAGER DEMO — disciplined sizing vs. actual paper-account behavior")
    print("=" * 78)

    for sym, note in [
        ("MARA", "worst historical performer: -$1,561.99 P&L / 35 trades, saw a 41,768-share position"),
        ("AMD", "best historical performer: +$853.79 P&L / 19 trades, sizes 5-840 shares"),
    ]:
        print(f"\n--- {sym} ({note}) ---")
        out = demo(sym)
        print(out.to_string(index=False))
        if len(out):
            print(f"\n  {sym} share-count range under disciplined sizing: "
                  f"{out['shares'].min():,} - {out['shares'].max():,} shares "
                  f"(vs. actual account range that included 41,768 shares on MARA alone)")
            print(f"  {sym} avg stop distance: {out['stop_dist_pct'].mean():.2%} of price "
                  f"(ATR-based, 2x multiple, 14-day window)")

    # --- Portfolio constraint example ---
    print("\n" + "=" * 78)
    print("PORTFOLIO CONSTRAINTS DEMO")
    print("=" * 78)
    pc = PortfolioConstraints(max_positions=8, max_symbol_weight=0.15, daily_loss_limit_pct=0.03)
    equity = 100_000

    mara_df = _load("MARA")
    entry_price = float(mara_df["close"].iloc[-1])
    stop = atr_stop(mara_df, entry_price, direction="long")
    sized = position_size(equity, entry_price, stop["stop_price"], risk_pct=0.01)
    check = pc.can_open_new_position("MARA", sized["position_dollars"], equity)
    print(f"\nProposed MARA position: {sized['shares']:,} shares (${sized['position_dollars']:,}, "
          f"{sized['pct_equity']:.2%} of equity)")
    print(f"Portfolio check: {check}")

    if check["allowed"]:
        pc.register_fill("MARA", sized["position_dollars"])

    # Simulate a bad day to show the kill switch
    pc.update_daily_pnl(-0.035)
    check2 = pc.can_open_new_position("AMD", 5_000, equity)
    print(f"\nAfter a -3.5% daily P&L move, proposing a new AMD position:")
    print(f"Portfolio check: {check2}")

    # Vol-aware risk sizing hook example
    print("\n" + "=" * 78)
    print("VOL-AWARE RISK SIZING HOOK (uses ml_ensemble_vol.py's predicted probability)")
    print("=" * 78)
    for proba in (None, 0.3, 0.7, 0.95):
        adj = vol_adjusted_risk_pct(0.01, proba)
        print(f"  vol_expansion_proba={proba} -> risk_pct={adj:.4f} "
              f"({'unchanged, no model input' if proba is None else f'{(1 - adj/0.01):.0%} reduction'})")
