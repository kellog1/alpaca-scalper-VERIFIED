# v4.2 — Broker Reconciliation, Restart Fix, Realistic Backtest Risk, Strategy Findings

## Defects fixed

- **`resync()` crashed on every restart.** It called `OpenPosition(...)` with the wrong number/order of positional args (a `datetime` landed in the `atr_at_entry` slot, `signal_price` was missing entirely), raising `TypeError` inside a bare `except Exception`. Position recovery after any restart was silently a no-op. Fixed, and it now falls back to percentage-based stop/target on resync since no bar history survives a restart to compute ATR from.
- **No reconciliation with broker-side bracket fills.** The bot never subscribed to Alpaca's trade-updates stream, so when a take-profit or stop-loss leg filled server-side, the bot never found out: the position stayed in `self.positions` forever (blocking new entries for that symbol) and the trade was never logged. Fixed by subscribing to `TradingStream` (`scalper/execution.py: on_trade_update`, wired in `scalper/bot.py`), which now reconciles broker fills back into position tracking and the trade log with the real fill price and reason (`target`/`stop`).
- **`flatten_all()` logged fabricated $0 P&L.** EOD-flatten and daily-loss-halt closes logged the position's own entry price as the "exit" price, so every such trade showed exactly 0.0 P&L regardless of the real fill. Fixed — closes now defer logging until the trade-updates fill confirms the real price (falls back to entry only in dry-run, where there's no real fill).
- **`close_position()` logged a fake close even when the broker call failed.** The position was popped from tracking and a "closed" trade was written to the DB unconditionally, even if `self.trading.close_position()` raised. Fixed: on failure, the position is restored into tracking instead of being silently dropped.

## Backtest now mirrors live risk management

`backtest.py`'s `run_backtest()` previously had **no leverage/exposure cap, no `max_concurrent_positions` cap, no daily-loss halt, and no session-window gating** — it just sized positions off `equity * risk_pct / stop_distance` with no ceiling, which could hugely oversize positions when volatility (and therefore stop distance) was small. This produced wildly unrealistic backtest results (e.g. -100% blowups) that would never happen live, since `RiskManager` enforces all of the above in `main.py`. Fixed: the backtest now instantiates and calls the real `RiskManager` for sizing, `can_open`, session state, and daily halt/flatten, so results reflect what the live bot would actually do.

## This session's strategy findings (not performance claims — reproduce before trusting)

- **The live breakout strategy (30s bars) is net-negative after transaction costs in every parameter variant tested** this session (tighter entry filters, wider ATR stop, longer max-hold) — profit factor stayed in the 0.28-0.65 range, with round-trip costs (~7-10bps) consistently exceeding the raw edge (which was itself only ~0.01-0.02% per trade in bps terms — roughly an order of magnitude short of the cost floor).
- **A VWAP mean-reversion sketch** (`scalper/signals.py: MeanReversionSignalEngine`, 5-min bars, fades extensions from session VWAP) was added and backtested. After fixing an unrelated bug (bar-close detection broke once its internal bar buffer hit `maxlen`, silently suppressing all but the first trading day's signals), a real sample (1,035 trades) showed **negative gross P&L before costs** — worse than the breakout strategy, not better. Not recommended as-is.
- **A daily-bar Donchian-breakout swing idea** (`backtest_swing.py`, new, separate from `backtest.py`) showed the only positive result all session: profit factor 1.27, +18.83% net over a 2-year/6-symbol sample, with costs collapsing to ~$34/trade (vs. $66-140/trade intraday) since round trips are far less frequent relative to the size of the moves captured. Positive on both long and short sides, not just riding the bull run in the sample window. **Small sample, single window, un-optimized parameters — promising lead, not validated.** SOXL/SOXS were deliberately excluded from this one (leveraged-ETF volatility decay unsuits multi-day holds).

# v4.1 — Filters, Fixes, and Cost Model

## What's implemented

**VWAP side filter** (`use_vwap_filter`): longs only above session VWAP, shorts only below. Implemented as a running per-symbol accumulator over volume-weighted typical price ((H+L+C)/3) that resets when the session date changes — a true session VWAP, not a rolling-window approximation.

**Higher-timeframe trend filter** (`use_htf_trend_filter`): 5-minute bars are aggregated internally from the 30-second bars the engine already receives, so the filter behaves identically live and in backtest. Direction is HTF close vs EMA(20) of HTF closes; entries against the HTF trend are blocked. While warming up (20 HTF bars = ~100 minutes at the 5-min default) the filter abstains and does not block — lower `htf_ema_period` if you want it active sooner.

**ATR-adaptive TP/SL** (`atr_tp_mult` / `atr_sl_mult`): targets and stops are ATR multiples captured at entry (default 2.0x / 1.0x, a constant 2:1 reward:risk across volatility regimes). Set both to 0 to fall back to the fixed-percentage exits.

**Transaction cost model** (backtest only, `costs:` in config): every simulated trade pays half-spread + slippage per side, with extra slippage on stop exits. The report now shows GROSS P&L, total costs, NET P&L, and what fraction of gross profit costs consumed. Net numbers are the only ones that matter.

## Honest limitations

- **No performance claims are made for any of these filters.** Whether they help is exactly what `backtest.py` exists to measure, on your symbols and date ranges. A prior version of this document contained estimated improvement figures presented as results; they were removed because they were never measured.
- The cost model is a static estimate. Real slippage on volume-spike breakouts is regime-dependent and usually worse than average. Compare live paper fills against modeled costs from day one — that gap is the most important number the paper phase produces.
- The backtest replays 1-minute bars against a 30-second-bar strategy and fills at computed prices with no partial fills or queueing.
- HTF filtering in backtest inherits the same warmup: the first ~100 minutes of each test window trade without the HTF gate.

## Not yet implemented (recommended before real capital, in order)

1. Out-of-sample split and walk-forward validation
2. Parameter sensitivity sweeps (a result that vanishes at ±1 parameter step is noise)
3. Correlation-aware exposure caps (NVDA+AMD+SOXL ≈ one bet) and reduced sizing for leveraged ETFs
4. Marketable limit orders instead of market orders
5. Halt/LULD handling
6. Written kill criteria
