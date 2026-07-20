"""Backtest the strategy on historical 1-minute bars from Alpaca.

Usage:
    python backtest.py --days 10
    python backtest.py --start 2026-06-01 --end 2026-07-01

Caveats (read these): this replays 1-min bars against a strategy designed for
30-second bars, fills at bar close with zero slippage or commission modeling,
and assumes full fills. Results are an OPTIMISTIC upper bound — a sanity check
for parameters, not a forecast.
"""
import argparse
from datetime import datetime, timedelta, timezone

from scalper.config import load_config, load_credentials
from scalper.risk import RiskManager
from scalper.signals import SignalEngine, MeanReversionSignalEngine, Side


class SimPosition:
    def __init__(self, side, entry, qty, ts):
        self.side, self.entry, self.qty, self.ts = side, entry, qty, ts
        self.trail_armed = False
        self.trail_stop = None
        self.target = None  # will be set based on ATR or percentage
        self.stop = None    # will be set based on ATR or percentage


USE_MEAN_REVERSION = True  # toggle to validate signals.MeanReversionSignalEngine


def run_backtest(cfg, bars_by_symbol):
    e = cfg["mean_reversion"] if USE_MEAN_REVERSION else cfg["exits"]
    # Use ATR multiples if available, else fall back to percentages
    use_atr_mode = e.get("atr_tp_mult", 0) > 0 and e.get("atr_sl_mult", 0) > 0
    atr_tp_mult = e.get("atr_tp_mult", 2.0)
    atr_sl_mult = e.get("atr_sl_mult", 1.0)
    tp_pct, sl_pct = e.get("profit_target_pct", 0.004), e.get("hard_stop_pct", 0.0025)
    trail_act, trail_by = e["trail_activate_pct"], e["trail_by_pct"]
    max_hold = e["max_hold_seconds"]

    # Transaction cost model (charged per side, in basis points of price)
    c = cfg.get("costs", {})
    half_spread = c.get("half_spread_bps", 1.5) / 10_000
    slip = c.get("slippage_bps", 2.0) / 10_000
    stop_extra = c.get("stop_extra_slippage_bps", 3.0) / 10_000
    per_side = half_spread + slip

    engine = MeanReversionSignalEngine(cfg) if USE_MEAN_REVERSION else SignalEngine(cfg)
    risk = RiskManager(cfg)
    equity = 100_000.0
    trades = []
    positions: dict[str, SimPosition] = {}
    last_price: dict[str, float] = {}
    trading_day = None
    flattened_today = False

    def _flatten(reason):
        nonlocal equity
        for s2 in list(positions):
            p2 = positions.pop(s2)
            exit_px = last_price.get(s2, p2.entry)
            gross = ((exit_px - p2.entry) if p2.side == Side.LONG
                     else (p2.entry - exit_px)) * p2.qty
            costs_paid = (p2.entry * per_side + exit_px * per_side) * p2.qty
            pnl = gross - costs_paid
            equity += pnl
            trades.append({"symbol": s2, "side": p2.side.value, "pnl": pnl,
                           "gross": gross, "costs": costs_paid, "reason": reason})

    # merge all bars into one time-ordered event stream
    events = []
    for sym, bars in bars_by_symbol.items():
        for b in bars:
            events.append((b["ts"], sym, b))
    events.sort(key=lambda x: x[0])

    for ts, sym, bar in events:
        last_price[sym] = bar["close"]

        day = ts.astimezone(risk.tz).date()
        if day != trading_day:
            trading_day = day
            risk.start_of_day(equity)
            flattened_today = False

        session = risk.session_state(ts)
        limits = risk.check_daily_limits(equity)
        if (limits["halt"] or session == "flatten") and not flattened_today:
            _flatten("daily_loss_halt" if limits["halt"] else "eod_flatten")
            flattened_today = True

        pos = positions.get(sym)
        if pos:
            exit_px, reason = None, None
            if pos.side == Side.LONG:
                if bar["low"] <= pos.stop:
                    exit_px, reason = pos.stop, "stop"
                elif bar["high"] >= pos.target:
                    exit_px, reason = pos.target, "target"
                else:
                    gain = (bar["close"] - pos.entry) / pos.entry
                    if not pos.trail_armed and gain >= trail_act:
                        pos.trail_armed, pos.trail_stop = True, bar["close"] * (1 - trail_by)
                    elif pos.trail_armed:
                        pos.trail_stop = max(pos.trail_stop, bar["close"] * (1 - trail_by))
                        if bar["low"] <= pos.trail_stop:
                            exit_px, reason = pos.trail_stop, "trail"
            else:
                if bar["high"] >= pos.stop:
                    exit_px, reason = pos.stop, "stop"
                elif bar["low"] <= pos.target:
                    exit_px, reason = pos.target, "target"
                else:
                    gain = (pos.entry - bar["close"]) / pos.entry
                    if not pos.trail_armed and gain >= trail_act:
                        pos.trail_armed, pos.trail_stop = True, bar["close"] * (1 + trail_by)
                    elif pos.trail_armed:
                        pos.trail_stop = min(pos.trail_stop, bar["close"] * (1 + trail_by))
                        if bar["high"] >= pos.trail_stop:
                            exit_px, reason = pos.trail_stop, "trail"

            if exit_px is None and (ts - pos.ts).total_seconds() >= max_hold:
                exit_px, reason = bar["close"], "time"

            if exit_px is not None:
                gross = ((exit_px - pos.entry) if pos.side == Side.LONG
                         else (pos.entry - exit_px)) * pos.qty
                # entry cost + exit cost; stop exits pay extra slippage
                exit_cost_rate = per_side + (stop_extra if reason == "stop" else 0.0)
                costs_paid = (pos.entry * per_side + exit_px * exit_cost_rate) * pos.qty
                pnl = gross - costs_paid
                equity += pnl
                trades.append({"symbol": sym, "side": pos.side.value,
                               "pnl": pnl, "gross": gross, "costs": costs_paid,
                               "reason": reason})
                del positions[sym]
            continue  # don't enter on the same bar we manage an open position

        sig = engine.on_bar(sym, bar)
        if sig and sym not in positions and risk.can_open(len(positions), session):
            atr_val = sig.atr
            if use_atr_mode:
                sl_dist = atr_val * atr_sl_mult
            else:
                sl_dist = sig.price * sl_pct
            open_position_value = sum(p.qty * p.entry for p in positions.values())
            qty = risk.position_size(equity, equity * risk.max_leverage, sig.price,
                                     sig.price - sl_dist if sig.side == Side.LONG
                                     else sig.price + sl_dist,
                                     open_position_value, session)
            if qty > 0:
                if use_atr_mode:
                    if sig.side == Side.LONG:
                        target = sig.price + atr_val * atr_tp_mult
                        stop = sig.price - atr_val * atr_sl_mult
                    else:
                        target = sig.price - atr_val * atr_tp_mult
                        stop = sig.price + atr_val * atr_sl_mult
                else:
                    if sig.side == Side.LONG:
                        target = sig.price * (1 + tp_pct)
                        stop = sig.price * (1 - sl_pct)
                    else:
                        target = sig.price * (1 - tp_pct)
                        stop = sig.price * (1 + sl_pct)
                pos = SimPosition(sig.side, sig.price, qty, ts)
                pos.target = target
                pos.stop = stop
                positions[sym] = pos

    return equity, trades


def print_report(start_equity, equity, trades):
    if not trades:
        print("No trades generated. Loosen filters or extend the date range.")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    pf = gross_w / gross_l if gross_l else float("inf")
    total_gross = sum(t.get("gross", t["pnl"]) for t in trades)
    total_costs = sum(t.get("costs", 0.0) for t in trades)
    net = equity - start_equity
    print(f"\n{'='*56}\nBACKTEST REPORT")
    print(f"Trades: {len(trades)}  Win rate (net): {len(wins)/len(trades)*100:.1f}%  "
          f"Profit factor (net): {pf:.2f}")
    print(f"GROSS P&L: {total_gross:+,.2f}")
    print(f"COSTS:     {-total_costs:,.2f}  "
          f"({total_costs/len(trades):.2f}/trade — spread + slippage model)")
    print(f"NET P&L:   {net:+,.2f}  ({(equity/start_equity - 1)*100:+.2f}%)  "
          f"Final equity: {equity:,.2f}")
    if total_gross > 0:
        print(f"Costs consumed {total_costs/total_gross*100:.0f}% of gross profit."
              if total_costs < total_gross else
              "Costs EXCEED gross profit — strategy is unprofitable net of costs.")
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["reason"], []).append(t["pnl"])
    for r, pnls in sorted(by_reason.items()):
        print(f"  exit={r:<7} n={len(pnls):<4} net_pnl={sum(pnls):+,.2f}")
    print("NOTE: cost model is an estimate (see config: costs). Live fills on")
    print("volume-spike breakouts are often worse than modeled, rarely better.")
    print("=" * 56)


def main():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--start")
    p.add_argument("--end")
    args = p.parse_args()

    cfg = load_config(args.config).raw
    creds = load_credentials()
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)

    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
    start = datetime.fromisoformat(args.start) if args.start \
        else end - timedelta(days=args.days)

    print(f"Fetching 1-min bars {start.date()} → {end.date()} "
          f"for {len(cfg['symbols'])} symbols...")
    req = StockBarsRequest(symbol_or_symbols=cfg["symbols"],
                           timeframe=TimeFrame.Minute, start=start, end=end,
                           feed=DataFeed.IEX)
    data = client.get_stock_bars(req)

    bars_by_symbol = {}
    for sym in cfg["symbols"]:
        if sym not in data.data:
            continue
        bars_by_symbol[sym] = [
            {"ts": b.timestamp, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for b in data.data[sym]
        ]
        print(f"  {sym}: {len(bars_by_symbol[sym])} bars")

    equity, trades = run_backtest(cfg, bars_by_symbol)
    print_report(100_000.0, equity, trades)


if __name__ == "__main__":
    main()
