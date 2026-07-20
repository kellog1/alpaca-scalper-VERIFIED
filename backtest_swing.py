"""Backtest a daily-bar Donchian-breakout swing strategy.

Candidate signal after the intraday scalper (breakout, 30s bars) and the
mean-reversion sketch (VWAP fade, 5-min bars) both failed to clear
transaction costs — this trades far less often on much larger moves, so the
same ~7-10bps round-trip cost is a small fraction of the edge per trade
instead of the dominant term.

Caveats (same honesty as backtest.py): fills assumed at bar close with no
slippage beyond the bps cost model, full fills assumed, no partial fills.
SOXL/SOXS are deliberately excluded — 3x leveraged, daily-reset ETFs decay
on volatility over multi-day holds and don't suit a swing approach even
though they were fine intraday.

Usage:
    python backtest_swing.py --days 400
"""
import argparse
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from scalper.config import load_credentials
from scalper.indicators import atr, avg_volume, rolling_high, rolling_low

SYMBOLS = ["TSLA", "NVDA", "AMD", "MARA", "SPY", "QQQ"]

LOOKBACK_DAYS = 20
VOL_AVG_PERIOD = 20
VOL_SPIKE_MULT = 1.5
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TP_MULT = 5.0
MAX_HOLD_DAYS = 20
RISK_PCT = 0.02
MAX_LEVERAGE = 2.0
MAX_POSITIONS = 4

HALF_SPREAD_BPS = 1.5
SLIPPAGE_BPS = 2.0
STOP_EXTRA_BPS = 3.0


def run_backtest(bars_by_symbol):
    per_side = (HALF_SPREAD_BPS + SLIPPAGE_BPS) / 10_000
    stop_extra = STOP_EXTRA_BPS / 10_000

    equity = 100_000.0
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
            if exit_px is None and (i - pos["entry_idx"]) >= MAX_HOLD_DAYS:
                exit_px, reason = bar["close"], "time"

            if exit_px is not None:
                gross = ((exit_px - pos["entry"]) if pos["side"] == "long"
                         else (pos["entry"] - exit_px)) * pos["qty"]
                exit_rate = per_side + (stop_extra if reason == "stop" else 0.0)
                costs = (pos["entry"] * per_side + exit_px * exit_rate) * pos["qty"]
                pnl = gross - costs
                equity += pnl
                trades.append({"symbol": sym, "side": pos["side"], "pnl": pnl,
                               "gross": gross, "costs": costs, "reason": reason})
                del positions[sym]
            continue

        if len(positions) >= MAX_POSITIONS:
            continue

        hist = bars[:i + 1]
        a = atr(hist, ATR_PERIOD)
        v_avg = avg_volume(hist, VOL_AVG_PERIOD)
        hi = rolling_high(hist, LOOKBACK_DAYS)
        lo = rolling_low(hist, LOOKBACK_DAYS)
        if None in (a, v_avg, hi, lo) or bar["volume"] < VOL_SPIKE_MULT * v_avg:
            continue

        side = "long" if bar["close"] > hi else "short" if bar["close"] < lo else None
        if side is None:
            continue

        entry = bar["close"]
        if side == "long":
            stop, target = entry - a * ATR_SL_MULT, entry + a * ATR_TP_MULT
        else:
            stop, target = entry + a * ATR_SL_MULT, entry - a * ATR_TP_MULT

        risk_dollars = equity * RISK_PCT
        qty = int(risk_dollars / abs(entry - stop))
        notional = sum(p["qty"] * p["entry"] for p in positions.values())
        qty = min(qty, int(max(0.0, MAX_LEVERAGE * equity - notional) / entry))
        if qty <= 0:
            continue

        positions[sym] = {"side": side, "entry": entry, "qty": qty, "stop": stop,
                          "target": target, "entry_idx": i}

    return equity, trades


def print_report(start_equity, equity, trades):
    if not trades:
        print("No trades generated. Loosen filters or extend --days.")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    pf = gross_w / gross_l if gross_l else float("inf")
    total_gross = sum(t["gross"] for t in trades)
    total_costs = sum(t["costs"] for t in trades)
    net = equity - start_equity
    print(f"\n{'='*56}\nSWING BACKTEST REPORT")
    print(f"Trades: {len(trades)}  Win rate: {len(wins)/len(trades)*100:.1f}%  "
          f"Profit factor: {pf:.2f}")
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400)
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

    equity, trades = run_backtest(bars_by_symbol)
    print_report(100_000.0, equity, trades)


if __name__ == "__main__":
    main()
