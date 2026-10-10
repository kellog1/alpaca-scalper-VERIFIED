# Handoff: alpaca-scalper-VERIFIED — Risk-Managed Baseline Pipeline

Context for picking this project up in Claude Code (or any fresh session) without losing what's already been established here. Read this before making changes.

## Ultimate goal

Eventually trade real capital. This is not a demo or a learning exercise — every design decision below (walk-forward validation, no reactive tuning, explicit edge-checking) is in service of that bar.

## What exists and where

**This repo** (`kellog1/alpaca-scalper-VERIFIED`) already contains a separate, working intraday momentum-breakout scalper (`main.py`, `scalper/`) — reviewed this session, found legitimate (official `alpaca-py` SDK, paper-only, proper risk limits, no exfiltration). Leave it alone; it's a different strategy with its own config.

**The new pipeline** (the 8 files delivered alongside this doc) belongs in a `baseline_pipeline/` subfolder so it doesn't collide with the existing bot's files or env var names:

- `feature_extractor.py` — engineered features (momentum, realized vol, ATR, ADX/DI, Bollinger, relative volume, cross-sectional vs. SPY)
- `ml_ensemble_vol.py` — the volatility-expansion classifier (the ONE validated edge — see below)
- `risk_manager.py` — `position_size()` (fixed-fractional sizing), `atr_stop()`, `PortfolioConstraints` (max positions, max symbol weight, daily-loss kill switch), `vol_adjusted_risk_pct()`
- `order_router.py` — pure order-payload construction + position reconciliation (no network calls)
- `baseline_pipeline.py` — ties it together: fixed basket (SPY, QQQ, AAPL, MSFT, NVDA), vol-model-adjusted sizing/stops, logs every run to `pipeline_log.csv`
- `alpaca_client.py` — standalone real HTTP client (reads `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` env vars — note different names than the existing bot's `.env` to avoid collision)
- `run_daily.py` — entry point; `DRY_RUN=True` by default, only submits orders if `RUN_DAILY_DRY_RUN=false` is explicitly set
- `requirements.txt`

## Honest edge findings — do not skip this

Walk-forward / chronological validation was run on 4.75 years of data across 37 symbols (2021-12-16 to 2026-09-29) before any of this was trusted:

- **ML direction models**: no real edge. v1 (100 bars, 6 symbols) looked great (profit factor 13-22x) but was single-regime bull-market bias — a "dumb buy everything" baseline would've looked just as good. v2 (4.75yr, 37 symbols, proper walk-forward) confirmed it: mean AUC 0.521, essentially chance.
- **Rules-based trend-following signal** (MA spread + ADX regime + momentum filter): also no edge. Underperformed buy-and-hold by 3.6 bps/day, beat it in only 4/10 chronological periods.
- **Volatility-expansion model** (`ml_ensemble_vol.py`): the only genuinely validated signal. Mean AUC 0.712, beat baseline in all 10 walk-forward folds.
  - **Caveat (2026-10-10): those numbers predate a label fix and must be re-run.** The old label measured vol over days t+10..t+19 (not the documented t+1..t+10) and scored the unknown final 10 rows of every symbol as 0 instead of dropping them. `vol_label.py` now defines the target once for both the trainer and `trade_analytics.py`. Re-run `ml_ensemble_vol.py` on the full history (the `data/*.csv` files are not in this repo) before relying on the 0.712 figure. **It predicts volatility, not direction** — used correctly here as a risk-sizing overlay (`vol_adjusted_risk_pct()` shrinks risk when expansion is predicted), never for entry/exit direction.

Because of this, the live strategy is deliberately **not claiming alpha**: it's a risk-managed, always-long baseline in 5 liquid names, decoupling "prove the automation infrastructure works" from "find real edge" (which continues separately). Don't reintroduce the direction model or the trend rule as a live signal without new evidence — both were tested honestly and failed.

## Hard constraint — applies to any Claude session, not just this one

**Claude does not submit trades itself, ever, even on a paper account.** This isn't a technical limitation, it's an operating rule with no paper-trading exception. `run_daily.py` is built around this: it's designed to be run under YOUR OWN scheduling (Task Scheduler, GitHub Actions, cron), using YOUR OWN credentials, so the actual `submit_order()` call happens as your process, not as an action any Claude session takes on your behalf. Any future Claude session working on this repo should preserve that separation — compute and report, never execute.

## Decisions already made (don't relitigate without new information)

- Semi-automated execution to start, graduating to fully unattended once real fills/slippage/vol-calibration have been watched for a while
- Daily automated *validation* (Trade Analytics, `trade_analytics.py` — included in this zip) is fine to run unattended since it's read-only
- No reactive daily strategy tuning — parameter changes are deliberate, evidence-based, proposed (not auto-applied), and gated on accumulating enough real trades to distinguish signal from noise
- API keys pasted into chat earlier this session (`PKR5HJ2644PA5BQGTED4IPTUIE` / ...) should be treated as compromised — rotate them in the Alpaca dashboard before relying on this long-term, and use the new ones only via environment variables, never hardcoded

## Immediate next steps

1. Create `baseline_pipeline/` in this repo, add these files
2. `pip install -r baseline_pipeline/requirements.txt`
3. Rotate Alpaca keys; set `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` (locally as env vars, or as GitHub Actions repo secrets if running via Actions)
4. Run `python baseline_pipeline/run_daily.py` with the default dry run — confirm output looks sane
5. Add a `.github/workflows/` cron (or local Task Scheduler) — keep `DRY_RUN=true` until you've watched several dry runs
6. Once comfortable, flip to live-paper submission; start accumulating real fills for `trade_analytics.py` to validate against

## Where the fuller history lives

The Claude.ai Project attached to this work (`alpaca-scalper-VERIFIED`) has the complete session history, including the original v1/v2 ML model code (kept for reference, not used live) and the signal generator's documented negative result.
