# First live-paper run checklist

The pipeline defaults to dry-run. Submitting orders requires setting the
`RUN_DAILY_DRY_RUN` repo variable to `false` yourself. Claude sessions never
submit orders.

## Before flipping the switch
- [ ] At least 3 to 5 weekday cron dry-runs with sane output: 5 targets, no surprises in `data/pipeline_log.csv`.
- [ ] Alpaca keys are for the **paper** account (the client is hardcoded to `paper-api.alpaca.markets`).
- [ ] Account sharing: prefer a separate paper account. If the intraday scalper shares it, confirm `reserved_symbols` in `config.yaml` lists the pipeline basket, so the scalper skips those names, ignores other positions on re-sync, and cancels only its own orders at EOD. The scalper's daily-loss halt and profit lock use its own P&L, so pipeline P&L can't trigger them. Caveat that remains: buying power is shared (the scalper uses up to 4x intraday leverage).
- [ ] Timing: scheduled runs can fire hours late (observed 5-6h), so the run itself guards on Alpaca's market clock: in live mode it submits nothing (no orders, no stop changes) when the market is closed, within 15 min of the close, or the clock can't be read. Dry-runs print `Market guard: OK/BLOCKED`. Still do the first live run manually at midday while watching.
- [ ] A guarded run shows `MARKET GUARD: no stop changes or orders submitted this run.` in the log; that is expected, not a failure. The next run picks up.
- [ ] Earnings: `baseline_pipeline/earnings_dates.csv` is current (hand-maintained; a past date is treated as unknown and warned about). The log shows `next_earnings` / `days_to_earnings` per name, and the run prints `EARNINGS SOON` within 3 days. Report-only: it does not change sizing, stops or orders, so decide yourself whether to hold off entering a name just before it reports.
- [ ] Log-commit push to `main` still works (a rejected push fails the run after orders are sent).
- [ ] No stray open orders in the Alpaca dashboard.

## The flip
- [ ] Set repo variable `RUN_DAILY_DRY_RUN=false` (Settings > Secrets and variables > Actions > Variables).
- [ ] Trigger the workflow manually ("Run workflow") during market hours.
- [ ] Immediately afterwards set the variable back to `true`, so the scheduled cron stays in dry-run until you decide otherwise.

## Verify the run (log plus Alpaca dashboard)
- [ ] Log shows `DRY_RUN=False` and 5 submissions with `status=200` (4xx = rejection; read the `error` text).
- [ ] Each entry accepted as `oto` + GTC. **This is untested against Alpaca.**
- [ ] Market orders filled. Record fill price vs. logged entry price for slippage.
- [ ] Each stop leg is open: type stop, side sell, at the logged stop price, qty = position.
- [ ] Stops are still open the next morning (did not expire at the close).
- [ ] Non-basket positions (ARE, MPC, WBD, WDAY) unchanged, no orders sent for them.
- [ ] Sizing: risk to stop is at most ~0.5% of equity per name (less after the vol overlay), `capped_by` in the log is `risk` for most names (a tight-stop name like SPY may show `max_position_pct`, cap 20%), and gross exposure is roughly 60% of equity, never above 100%.
- [ ] `data/order_submission_log.csv` committed.

## Next day (manual run, still dry-run)
- [ ] Positions within tolerance of targets: no add/reduce orders.
- [ ] "0 stop action(s)", or a sensible ratchet up. A stop is never lowered.
- [ ] No duplicate stops per symbol.

## Abort and rollback
- [ ] Any rejected or partial order: stop, leave dry-run on, review the log.
- [ ] Unwanted positions: cancel open orders, then flatten in the Alpaca dashboard. Do not rely on the script to unwind.
- [ ] Not yet verified: stop replace via PATCH, settle timing after cancel, vol-model calibration. The first days of live data test these.

## After 2 to 3 weeks of fills
- [ ] Run `trade_analytics.py` on fills + log: slippage, stop discipline, vol calibration.
- [ ] Change parameters only on evidence, proposed before applying.
