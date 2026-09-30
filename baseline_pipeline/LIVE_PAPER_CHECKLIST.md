# First live-paper run checklist

The pipeline defaults to dry-run. Submitting orders requires setting the
`RUN_DAILY_DRY_RUN` repo variable to `false` yourself. Claude sessions never
submit orders.

## Before flipping the switch
- [ ] At least 3 to 5 weekday cron dry-runs with sane output: 5 targets, no surprises in `data/pipeline_log.csv`.
- [ ] Alpaca keys are for the **paper** account (the client is hardcoded to `paper-api.alpaca.markets`).
- [ ] Account sharing: prefer a separate paper account. If the intraday scalper shares it, confirm `reserved_symbols` in `config.yaml` lists the pipeline basket, so the scalper skips those names, ignores other positions on re-sync, and cancels only its own orders at EOD. Caveats that remain: shared buying power, and the scalper's daily-loss halt / profit lock read whole-account equity, so pipeline P&L can trigger them.
- [ ] Run during US market hours, not the 12:52 UTC cron (before the open, market orders queue until it opens). Do the first run manually at midday while watching.
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
- [ ] Sizing: ~$9-10k per name (~10% of equity), risk to stop ~1% of equity or less per name.
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
