"""Backtest a daily-bar Donchian-breakout swing strategy.

Candidate signal after the intraday scalper (breakout, 30s bars) and the
mean-reversion sketch (VWAP fade, 5-min bars) both failed to clear
transaction costs — this trades far less often on much larger moves, so the
same ~7-10bps round-trip cost is a small fraction of the edge per trade
instead of the dominant term.

A single full-range run (the default mode) showed a promising but UNVALIDATED
result: PF 1.27, +18.83% net over a 2-year/6-symbol sample. Single window,
un-optimized parameters — exactly the kind of result that can be noise. This
file also implements the validation IMPROVEMENTS.md called for:

  --sensitivity    Parameter sensitivity sweep: vary one knob at a time
                    around the defaults, full range, report PF/net for each.
                    A result that vanishes at +/-1 step is noise, not edge.
  --walk-forward    Chronological walk-forward validation: split the sample
                    into N folds, each an in-sample (IS) segment followed by
                    an out-of-sample (OOS) segment. Per fold, grid-search a
                    small parameter set on IS only (max net P&L, minimum
                    trade count), then apply the SELECTED params unmodified
                    to that fold's OOS segment. The aggregate OOS result
                    across all folds — never IS — is the walk-forward
                    verdict: it answers whether the edge holds up
                    out-of-sample across different time periods, not just in
                    the single window originally tested.

None of these modes can run in an environment without network access to
Alpaca's historical data API and valid credentials — this file provides the
harness; running it and reading the result is still required before trusting
the strategy.

Caveats (same honesty as backtest.py): fills assumed at bar close with no
slippage beyond the bps cost model, full fills assumed, no partial fills.
SOXL/SOXS are deliberately excluded — 3x leveraged, daily-reset ETFs decay
on volatility over multi-day holds and don't suit a swing approach even
though they were fine intraday.

Usage:
    python backtest_swing.py --days 400
    python backtest_swing.py --days 750 --sensitivity
    python backtest_swing.py --days 750 --walk-forward --folds 5
"""
import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from scalper.config import load_credentials
from scalper.indicators import atr, avg_volume, rolling_high, rolling_low

SYMBOLS = ["TSLA", "NVDA", "AMD", "MARA", "SPY", "QQQ"]


@dataclass(frozen=True)
class Params:
    lookback_days: int = 20
    vol_avg_period: int = 20
    vol_spike_mult: float = 1.5
    atr_period: int = 14
    atr_sl_mult: float = 2.0
    atr_tp_mult: float = 5.0
    max_hold_days: int = 20
    risk_pct: float = 0.02
    max_leverage: float = 2.0
    max_positions: int = 4
    half_spread_bps: float = 1.5
    slippage_bps: float = 2.0
    stop_extra_slippage_bps: float = 3.0


DEFAULT = Params()

# Warmup buffer (calendar days) prepended to each fold so indicators
# (max lookback ~atr_period/vol_avg_period/lookback_days, ~20 trading days)
# are past warmup by the fold's actual start date.
WARMUP_CALENDAR_DAYS = 60


def run_backtest(bars_by_symbol: dict, params: Params = DEFAULT,
                  start_equity: float = 100_000.0):
    per_side = (params.half_spread_bps + params.slippage_bps) / 10_000
    stop_extra = params.stop_extra_slippage_bps / 10_000

    equity = start_equity
    trades = []
    positions = {}

    events = []
    for sym, bars in bars_by_symbol.items():
        for i in range(len(bars)):
            events.append((bars[i]["ts"], sym, i))
    events.sort(key=lambda x: x[0])

    for ts, sym, i in events:
        bars = bars_by_symbol[sym]
        bar = bars[i]
        pos = positions.get(sym)

        if pos:
            exit_px, reason = None, None
            if pos["side"] == "long":
                if bar["low"] <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif bar["high"] >= pos["target"]:
                    exit_px, reason = pos["target"], "target"
            else:
                if bar["high"] >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif bar["low"] <= pos["target"]:
                    exit_px, reason = pos["target"], "target"
            if exit_px is None and (i - pos["entry_idx"]) >= params.max_hold_days:
                exit_px, reason = bar["close"], "time"

            if exit_px is not None:
                gross = ((exit_px - pos["entry"]) if pos["side"] == "long"
                         else (pos["entry"] - exit_px)) * pos["qty"]
                exit_rate = per_side + (stop_extra if reason == "stop" else 0.0)
                costs = (pos["entry"] * per_side + exit_px * exit_rate) * pos["qty"]
                pnl = gross - costs
                equity += pnl
                trades.append({"symbol": sym, "side": pos["side"], "pnl": pnl,
                               "gross": gross, "costs": costs, "reason": reason,
                               "entry_date": pos["entry_ts"].date(),
                               "exit_date": ts.date()})
                del positions[sym]
            continue

        if len(positions) >= params.max_positions:
            continue

        hist = bars[:i + 1]
        a = atr(hist, params.atr_period)
        v_avg = avg_volume(hist, params.vol_avg_period)
        hi = rolling_high(hist, params.lookback_days)
        lo = rolling_low(hist, params.lookback_days)
        if None in (a, v_avg, hi, lo) or bar["volume"] < params.vol_spike_mult * v_avg:
            continue

        side = "long" if bar["close"] > hi else "short" if bar["close"] < lo else None
        if side is None:
            continue

        entry = bar["close"]
        if side == "long":
            stop, target = entry - a * params.atr_sl_mult, entry + a * params.atr_tp_mult
        else:
            stop, target = entry + a * params.atr_sl_mult, entry - a * params.atr_tp_mult

        risk_dollars = equity * params.risk_pct
        qty = int(risk_dollars / abs(entry - stop))
        notional = sum(p["qty"] * p["entry"] for p in positions.values())
        qty = min(qty, int(max(0.0, params.max_leverage * equity - notional) / entry))
        if qty <= 0:
            continue

        positions[sym] = {"side": side, "entry": entry, "qty": qty, "stop": stop,
                          "target": target, "entry_idx": i, "entry_ts": ts}

    return equity, trades


# --------------------------------------------------------------- summaries --
def _summary(trades, start_equity=100_000.0):
    if not trades:
        return {"n": 0, "net": 0.0, "pf": None, "win_rate": None}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    return {
        "n": len(trades),
        "net": sum(t["pnl"] for t in trades),
        "pf": (gross_w / gross_l) if gross_l else float("inf"),
        "win_rate": len(wins) / len(trades) * 100,
    }


def print_report(start_equity, equity, trades):
    if not trades:
        print("No trades generated. Loosen filters or extend --days.")
        return
    s = _summary(trades, start_equity)
    total_gross = sum(t["gross"] for t in trades)
    total_costs = sum(t["costs"] for t in trades)
    net = equity - start_equity
    print(f"\n{'='*56}\nSWING BACKTEST REPORT")
    print(f"Trades: {s['n']}  Win rate: {s['win_rate']:.1f}%  Profit factor: {s['pf']:.2f}")
    print(f"GROSS P&L: {total_gross:+,.2f}")
    print(f"COSTS:     {-total_costs:,.2f}  ({total_costs/len(trades):.2f}/trade)")
    print(f"NET P&L:   {net:+,.2f}  ({(equity/start_equity - 1)*100:+.2f}%)  "
          f"Final equity: {equity:,.2f}")
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["reason"], []).append(t["pnl"])
    for r, pnls in sorted(by_reason.items()):
        print(f"  exit={r:<7} n={len(pnls):<4} net_pnl={sum(pnls):+,.2f}")
    for side in ("long", "short"):
        st = [t for t in trades if t["side"] == side]
        if not st:
            print(f"  side={side:<6} n=0")
            continue
        sw = [t for t in st if t["pnl"] > 0]
        print(f"  side={side:<6} n={len(st):<4} win_rate={len(sw)/len(st)*100:.1f}% "
              f"net_pnl={sum(t['pnl'] for t in st):+,.2f}")
    print("=" * 56)


# ---------------------------------------------------- parameter sensitivity --
SENSITIVITY_GRID = {
    "lookback_days": [15, 20, 25],
    "vol_spike_mult": [1.2, 1.5, 2.0],
    "atr_sl_mult": [1.5, 2.0, 2.5],
    "atr_tp_mult": [3.0, 5.0, 7.0],
    "max_hold_days": [10, 20, 30],
}


def run_sensitivity(bars_by_symbol):
    print(f"\n{'='*72}\nPARAMETER SENSITIVITY SWEEP (one knob at a time, others at default)")
    print(f"{'param':<16}{'value':>8}{'trades':>8}{'win%':>8}{'PF':>8}{'net%':>10}")
    base_equity, base_trades = run_backtest(bars_by_symbol, DEFAULT)
    base = _summary(base_trades)
    print(f"{'(default)':<16}{'':>8}{base['n']:>8}{base['win_rate'] or 0:>8.1f}"
          f"{base['pf'] or 0:>8.2f}{(base_equity/100_000-1)*100:>10.2f}")
    print("-" * 72)
    for name, values in SENSITIVITY_GRID.items():
        for v in values:
            params = replace(DEFAULT, **{name: v})
            eq, trades = run_backtest(bars_by_symbol, params)
            s = _summary(trades)
            marker = " <- default" if v == getattr(DEFAULT, name) else ""
            print(f"{name:<16}{v:>8}{s['n']:>8}{(s['win_rate'] or 0):>8.1f}"
                  f"{(s['pf'] or 0):>8.2f}{(eq/100_000-1)*100:>10.2f}{marker}")
    print("=" * 72)
    print("A row that flips sign or collapses PF toward the default's neighbors")
    print("means the full-range result is sensitive to that knob — treat it as")
    print("noise, not edge, until it's robust across the values shown.")


# -------------------------------------------------------- walk-forward mode --
WF_GRID = {
    "atr_sl_mult": [1.5, 2.0, 2.5],
    "atr_tp_mult": [3.0, 5.0, 7.0],
    "vol_spike_mult": [1.2, 1.5, 2.0],
}


def _grid_candidates():
    keys = list(WF_GRID)
    for combo in product(*(WF_GRID[k] for k in keys)):
        yield replace(DEFAULT, **dict(zip(keys, combo)))


def run_walk_forward(bars_by_symbol, folds: int, is_frac: float, min_trades: int):
    all_dates = [b["ts"].date() for bars in bars_by_symbol.values() for b in bars]
    start, end = min(all_dates), max(all_dates)
    total_days = (end - start).days
    chunk = total_days / folds

    print(f"\n{'='*72}\nWALK-FORWARD VALIDATION  ({folds} folds, IS={is_frac:.0%}/OOS={1-is_frac:.0%} "
          f"each, {len(list(_grid_candidates()))} param combos searched per fold)")
    print(f"Full sample: {start} -> {end} ({total_days} calendar days)")
    print("-" * 72)

    aggregate_oos = []
    for k in range(folds):
        chunk_start = start + timedelta(days=int(k * chunk))
        chunk_end = start + timedelta(days=int((k + 1) * chunk)) if k < folds - 1 else end
        is_end = chunk_start + timedelta(days=int((chunk_end - chunk_start).days * is_frac))
        buffer_start = chunk_start - timedelta(days=WARMUP_CALENDAR_DAYS)

        fold_bars = {sym: [b for b in bars if buffer_start <= b["ts"].date() < chunk_end]
                    for sym, bars in bars_by_symbol.items()}

        best_params, best_is_net, best_is_trades, best_oos_trades = None, None, [], []
        for params in _grid_candidates():
            _, trades = run_backtest(fold_bars, params)
            is_trades = [t for t in trades if chunk_start <= t["entry_date"] < is_end]
            oos_trades = [t for t in trades if is_end <= t["entry_date"] < chunk_end]
            if len(is_trades) < min_trades:
                continue
            is_net = sum(t["pnl"] for t in is_trades)
            if best_is_net is None or is_net > best_is_net:
                best_params, best_is_net = params, is_net
                best_is_trades, best_oos_trades = is_trades, oos_trades

        if best_params is None:
            # No candidate cleared the min-trade bar on this fold's IS window —
            # fall back to defaults rather than silently skipping the fold.
            _, trades = run_backtest(fold_bars, DEFAULT)
            best_params = DEFAULT
            best_is_trades = [t for t in trades if chunk_start <= t["entry_date"] < is_end]
            best_oos_trades = [t for t in trades if is_end <= t["entry_date"] < chunk_end]
            note = " (insufficient IS trades for any candidate; used defaults)"
        else:
            note = ""

        is_s, oos_s = _summary(best_is_trades), _summary(best_oos_trades)
        print(f"Fold {k+1}: IS [{chunk_start} -> {is_end}) n={is_s['n']} "
              f"pf={is_s['pf'] or 0:.2f} net={is_s['net']:+,.0f}{note}")
        print(f"        OOS [{is_end} -> {chunk_end}) n={oos_s['n']} "
              f"pf={oos_s['pf'] or 0:.2f} net={oos_s['net']:+,.0f}")
        print(f"        selected: sl={best_params.atr_sl_mult} tp={best_params.atr_tp_mult} "
              f"vol_mult={best_params.vol_spike_mult}")
        aggregate_oos.extend(best_oos_trades)

    print("-" * 72)
    agg = _summary(aggregate_oos)
    print(f"AGGREGATE OUT-OF-SAMPLE (the actual verdict — IS numbers above are")
    print(f"selection bias and don't count): n={agg['n']} "
          f"win_rate={(agg['win_rate'] or 0):.1f}% pf={(agg['pf'] or 0):.2f} "
          f"net={agg['net']:+,.2f}")
    if agg["n"] < 20:
        print("Fewer than 20 aggregate OOS trades — too small a sample to trust either way.")
    print("=" * 72)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--sensitivity", action="store_true",
                   help="Run the parameter sensitivity sweep instead of a single backtest.")
    p.add_argument("--walk-forward", action="store_true",
                   help="Run chronological walk-forward validation instead of a single backtest.")
    p.add_argument("--folds", type=int, default=4, help="Walk-forward fold count.")
    p.add_argument("--is-frac", type=float, default=0.6,
                   help="Fraction of each walk-forward fold used as in-sample.")
    p.add_argument("--min-trades", type=int, default=8,
                   help="Minimum in-sample trades for a walk-forward candidate to be eligible.")
    args = p.parse_args()

    creds = load_credentials()
    client = StockHistoricalDataClient(creds.api_key, creds.secret_key)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    print(f"Fetching daily bars {start.date()} -> {end.date()} for {len(SYMBOLS)} symbols...")
    req = StockBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=TimeFrame.Day,
                           start=start, end=end, feed=DataFeed.IEX)
    data = client.get_stock_bars(req)

    bars_by_symbol = {}
    for sym in SYMBOLS:
        if sym not in data.data:
            continue
        bars_by_symbol[sym] = [
            {"ts": b.timestamp, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for b in data.data[sym]]
        print(f"  {sym}: {len(bars_by_symbol[sym])} daily bars")

    if args.sensitivity:
        run_sensitivity(bars_by_symbol)
    elif args.walk_forward:
        run_walk_forward(bars_by_symbol, args.folds, args.is_frac, args.min_trades)
    else:
        equity, trades = run_backtest(bars_by_symbol, DEFAULT)
        print_report(100_000.0, equity, trades)


if __name__ == "__main__":
    main()
