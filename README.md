# Aggressive Alpaca Paper Scalping Bot

A paper-trading scalping bot for Alpaca implementing an aggressive momentum-breakout strategy on 30-second bars. **Paper trading only — read the warnings section before doing anything else.**

## Strategy

Momentum breakouts on high-volatility names (TSLA, NVDA, AMD, MARA, SOXL/SOXS, SPY, QQQ):

- **Entry (long):** close breaks the prior 5-bar high, with bar volume > 2x the 20-bar average, EMA(5) > EMA(13), and bar range exceeding ATR(10). Shorts mirror this.
- **Exits:** 0.4% profit target, 0.25% hard stop (both server-side via bracket orders), plus client-side trailing stop (arms at +0.2%, trails by 0.1%) and a 3-minute time exit.
- **Re-entries:** up to 2 continuation entries per symbol per EMA-defined trend.

## Risk profile (aggressive)

- 2.5% of equity risked per trade, sized from stop distance
- Up to 4x intraday exposure, max 6 concurrent positions
- Daily loss limit: -6% halts trading and flattens everything
- Profit lock: at +5% on the day, all stops tighten to breakeven
- Trades the open (9:35–11:00 ET) and power hour (15:00–15:50 ET) at full size, midday at half size; flattens all positions 10 minutes before close

## Setup

1. Create an Alpaca account at https://alpaca.markets and generate **paper trading** API keys (top-right in the dashboard, switch to "Paper" first).
2. Install and configure:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your paper keys into .env
```

3. Sanity-check parameters against history:

```bash
python backtest.py --days 10
```

Note: `backtest.py` has a `USE_MEAN_REVERSION` flag near the top. It defaults to testing the mean-reversion sketch (see Strategy note below) rather than the breakout strategy `main.py` actually runs live — set it to `False` to backtest what's live. There's also `backtest_swing.py`, a separate daily-bar swing-breakout backtest (see below).

4. Watch signals without placing orders:

```bash
python main.py --dry-run
```

5. Run against the paper account, with the dashboard alongside:

```bash
python main.py          # terminal 1: the bot
python dashboard.py     # terminal 2: http://localhost:8080
```

The dashboard reads `scalper_log.db` (read-only) and auto-refreshes every 15s: cumulative P&L, drawdown, per-symbol results, exit-reason breakdown, and a live trade blotter.

All trades, signals, fills, and P&L are logged to `scalper_log.db` (SQLite); an end-of-day report (win rate, profit factor, avg hold, max drawdown) prints after the close.

## Architecture

```
main.py            entrypoint / CLI
backtest.py        historical check with transaction-cost model + risk sim
                    (USE_MEAN_REVERSION toggles which signal engine it tests)
backtest_swing.py  separate daily-bar Donchian-breakout swing backtest
dashboard.py       local web dashboard (stdlib, reads the SQLite log)
config.yaml        every tunable parameter (incl. mean_reversion: sketch config)
scalper/
  datastream.py    WebSocket ticks → 30s bars, auto-reconnect
  signals.py       SignalEngine (breakout+volume+EMA+ATR, live in main.py) and
                    MeanReversionSignalEngine (VWAP-fade sketch, backtest-only)
  risk.py          sizing, leverage cap, daily limits, sessions
  execution.py     bracket orders, trailing/time exits, rate throttle,
                    broker-side fill reconciliation (trade_updates stream)
  logger.py        SQLite + console logging, EOD report
  bot.py           orchestrator (asyncio)
```

On restart, the bot re-syncs open positions from the Alpaca API, so a crash mid-session doesn't orphan positions. It also subscribes to Alpaca's trade-updates stream so that bracket take-profit/stop-loss fills — which happen server-side, without the bot's own price-tick loop ever seeing them — get reconciled back into position tracking and the trade log instead of leaving a phantom open position.

**Backtesting note:** this session's backtests (see `IMPROVEMENTS.md` for details) found the live breakout strategy net-negative after transaction costs across every parameter variant tried, and the mean-reversion sketch net-negative even before costs. A separate daily-bar swing-breakout idea (`backtest_swing.py`) showed a positive, if unproven, result — see `IMPROVEMENTS.md`.

## ⚠️ Warnings — read this

- **This configuration is deliberately aggressive and would be dangerous with real money.** Risking 2.5% per trade with 4x leverage means a normal losing streak can hit the -6% daily halt fast, and several bad days compound severely.
- **Paper results are an optimistic upper bound.** Alpaca paper fills don't model slippage, partial fills, or queue position — and breakout entries on volume spikes are precisely where real slippage is worst. The backtest is even more optimistic (close-price fills, zero costs).
- **PDT rules:** live cash accounts under $25,000 are limited to 3 day trades per 5 business days. This bot would violate that immediately. Paper accounts aren't restricted, which is another way paper behavior diverges from reality.
- **SOXL/SOXS are 3x leveraged ETFs** — volatility on top of leverage on top of an aggressive risk profile.
- Nothing here is financial advice. This is an educational tool for learning how automated trading systems are structured and how they behave under stress.
