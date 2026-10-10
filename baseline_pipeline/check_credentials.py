"""
Preflight for the scheduled workflows: fail fast, with a readable reason, when
the Alpaca secrets are missing, malformed or rejected.

Reads the same variables as alpaca_client.py (ALPACA_API_KEY_ID /
ALPACA_API_SECRET_KEY), calls the two read-only endpoints the workflows depend
on, and prints only lengths and HTTP statuses. Key and secret values are never
printed. Exit code 0 = usable, 1 = not usable.
"""
from __future__ import annotations
import os
import sys

import requests

KEY_VAR, SECRET_VAR = "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"
CHECKS = [
    ("paper account", "https://paper-api.alpaca.markets/v2/account"),
    ("market data", "https://data.alpaca.markets/v2/stocks/SPY/trades/latest?feed=iex"),
]


def local_problems(key: str | None, secret: str | None) -> list[str]:
    """Problems visible without any network call."""
    problems = []
    for name, val in ((KEY_VAR, key), (SECRET_VAR, secret)):
        if not val:
            problems.append(f"{name} is empty or not set (check the repo secret exists and the name matches exactly)")
        elif val != val.strip():
            problems.append(f"{name} has leading/trailing whitespace or a newline; re-paste it without extra characters")
    return problems


def explain(status: int) -> str:
    if status in (401, 403):
        return ("rejected. The secrets are not a valid PAPER key pair: they may be stale (rotated/revoked), "
                "mismatched (key from one pair, secret from another), or live-account keys "
                "(this pipeline only talks to paper-api.alpaca.markets)")
    if status == 429:
        return "rate limited; retry the run shortly"
    if status >= 500:
        return "Alpaca returned a server error; retry the run shortly"
    return "unexpected response"


def main(env=os.environ, get=requests.get) -> int:
    key, secret = env.get(KEY_VAR), env.get(SECRET_VAR)
    problems = local_problems(key, secret)
    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1
    print(f"{KEY_VAR}: {len(key)} chars, {SECRET_VAR}: {len(secret)} chars")

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    ok = True
    for name, url in CHECKS:
        try:
            status = get(url, headers=headers, timeout=20).status_code
        except requests.RequestException as e:
            print(f"::error::{name}: request failed ({type(e).__name__})")
            ok = False
            continue
        print(f"{name}: HTTP {status}")
        if status != 200:
            print(f"::error::{name}: HTTP {status}, {explain(status)}")
            ok = False
    if ok:
        print("Alpaca credentials accepted.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
