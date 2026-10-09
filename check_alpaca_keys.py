"""Check that the Alpaca paper keys in .env are accepted, without printing them.

Usage:
    python check_alpaca_keys.py

Calls two read-only endpoints (paper account, latest IEX trade) and prints
only the HTTP status of each. Key and secret values are never printed.

Note: load_dotenv() does not override variables already set in the shell,
so stale ALPACA_* environment variables win over .env. This script warns
when it sees them.
"""
import os
import sys

import requests
from dotenv import load_dotenv

CHECKS = [
    ("paper account", "https://paper-api.alpaca.markets/v2/account"),
    ("market data", "https://data.alpaca.markets/v2/stocks/SPY/trades/latest?feed=iex"),
]


def main() -> int:
    stale = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") if k in os.environ]
    load_dotenv()
    if stale:
        print(f"warning: {', '.join(stale)} already set in the environment; "
              "those values take precedence over .env")
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
        return 2
    print(f"key length {len(key)}, secret length {len(secret)}, "
          f"key prefix {key[:2]!r}")

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    ok = True
    for name, url in CHECKS:
        status = requests.get(url, headers=headers, timeout=20).status_code
        print(f"{name}: {status}")
        ok &= status == 200
    print("keys accepted" if ok else "keys rejected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
