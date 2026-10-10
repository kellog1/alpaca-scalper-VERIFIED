# alpaca-scalper-VERIFIED

Two separate strategies share one Alpaca **paper** account:
- `main.py`, `scalper/`, `config.yaml`: intraday scalper. Its own backtests are net-negative after costs; leave it alone.
- `baseline_pipeline/`: the active project. Always-long SPY/QQQ/AAPL/MSFT/NVDA with ATR stops and a vol-model risk overlay, run by GitHub Actions. Start with `baseline_pipeline/HANDOFF.md`.

## Hard rule
Claude never submits orders, even on paper. `run_daily.py` runs under the owner's own scheduler and credentials. Compute, test and report only. Test harnesses must fake the Alpaca client.

## Tests (offline, plain-assert scripts, no pytest)
- `cd baseline_pipeline && for t in test_*.py; do python "$t"; done`
- `python test_shared_account.py` from the repo root.
- CI (`.github/workflows/tests.yml`) runs both on every PR. Python 3.11. Dependencies in both `requirements.txt` files are pinned exactly: bump them together and re-run the tests.

## Invariants (each has a test; do not regress)
- A missing target is not a zero target: `unmanaged_symbols`/`hold_unmanaged` leave such symbols untouched.
- The vol-expansion label lives only in `vol_label.py`; training and live scoring both import it. Unknown rows are NaN, never 0.
- A run must be safe to repeat (the workflow fires 4x/day, scheduled runs arrive hours late): symbols with open non-stop orders are left alone, and `log_run` writes once per UTC day.
- `check_credentials.py` runs first in both workflows. A 401 there means the repo secrets `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` are not a valid paper pair; only the repo owner can replace them.

## Git workflow
- Branch from `main` per change; open a PR or merge only when asked. Merge with a merge commit, pinned to `expectedHeadSha`, after checks pass.
- If `git push` returns 403 (GitHub App lacks write access), use the GitHub tools: `create_branch`, then `push_files` with whole files. Verify each file with `git hash-object` against the remote blob sha.

## Working conventions (the owner has a usage limit)
- Keep replies short; skip recaps of what the diff already shows.
- Batch related steps. Read only the lines needed; fetch logs with `tail_lines`. Don't print whole files unless about to push them.
- Don't subscribe to PR activity or schedule check-ins unless asked.
- Start unrelated work in a fresh session.

## Open items (as of 2026-10-10)
- The live model trains on ~2y of raw (unadjusted) bars, not the 4.75y it was validated on; splits would corrupt it.
- Workflow log-commit step uses a bare `git push` and can lose logs if `main` moves mid-run.
- Scalper and pipeline share one paper account (buying power, analytics contamination).
