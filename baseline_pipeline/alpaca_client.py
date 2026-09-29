"""
Standalone Alpaca REST client — for running on YOUR OWN machine or CI
runner, not in a Claude cloud sandbox (which cannot reach
paper-api.alpaca.markets or data.alpaca.markets directly; org egress
policy blocks it there).

This is real, unmediated network code: when you run it, it executes
directly as your own process with your own credentials. Nothing here goes
through Claude at runtime.

SECURITY:
  - Reads credentials from environment variables (ALPACA_API_KEY_ID,
    ALPACA_API_SECRET_KEY) — never hardcode keys in this file.
  - Any keys that were ever pasted into a chat should be considered
    compromised — rotate them in the Alpaca dashboard and set the new
    ones as environment variables instead.
  - Uses the PAPER endpoints only (paper-api.alpaca.markets /
    data.alpaca.markets with paper keys). Do not repoint BASE_URL at the
    live-trading host.
"""
from __future__ import annotations
import os
import time
import pandas as pd
import requests

TRADING_BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_BASE_URL = "https://data.alpaca.markets/v2"


def _headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY environment variables "
            "before running (rotate your keys in the Alpaca dashboard first if they "
            "were ever pasted into a chat)."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def get_account() -> dict:
    r = requests.get(f"{TRADING_BASE_URL}/account", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def get_positions() -> dict:
    """Returns {symbol: qty (float, signed)}."""
    r = requests.get(f"{TRADING_BASE_URL}/positions", headers=_headers(), timeout=15)
    r.raise_for_status()
    return {p["symbol"]: float(p["qty"]) for p in r.json()}


def get_fills(since_iso: str | None = None, limit: int = 100) -> list[dict]:
    params = {"activity_types": "FILL", "page_size": limit}
    if since_iso:
        params["after"] = since_iso
    r = requests.get(f"{TRADING_BASE_URL}/account/activities/FILL", headers=_headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def submit_order(payload: dict) -> dict:
    """
    THE ACTUAL TRADE-EXECUTION CALL. This is the one thing Claude itself
    will never invoke — calling this function is what makes this script
    yours, not Claude's. Runs under whatever DRY_RUN gating the caller
    (run_daily.py) applies.
    """
    r = requests.post(f"{TRADING_BASE_URL}/orders", headers=_headers(), json=payload, timeout=15)
    if r.status_code >= 400:
        return {"symbol": payload.get("symbol"), "status": r.status_code, "error": r.text}
    return {"symbol": payload.get("symbol"), "status": r.status_code, "order": r.json()}


def get_daily_bars(symbols: list[str], lookback_days: int = 760) -> dict[str, pd.DataFrame]:
    """
    Pulls daily OHLCV bars for a list of symbols from Alpaca's market data
    API (works on the paper-account keys; IEX feed is fine for daily bars).
    Returns {symbol: DataFrame[date-indexed, open/high/low/close/volume]},
    matching exactly what feature_extractor.py expects.

    Pulls ~760 calendar days (~2 trading years plus buffer) by default so
    the 200-day moving-average feature has enough history; re-pulling full
    history each run is simpler and more robust than incremental updates
    (no dedup/gap bugs), and well within Alpaca's rate limits for ~40 symbols
    once a day.
    """
    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=lookback_days)
    out = {}

    # Alpaca's multi-symbol bars endpoint handles a batch, but keep it
    # simple and robust with one request per symbol + basic rate-limit backoff.
    for sym in symbols:
        params = {
            "symbols": sym,
            "timeframe": "1Day",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "adjustment": "raw",
            "feed": "iex",
        }
        for attempt in range(3):
            r = requests.get(f"{DATA_BASE_URL}/stocks/bars", headers=_headers(), params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(f"Rate-limited fetching {sym} after 3 attempts")

        bars = r.json().get("bars", {}).get(sym, [])
        if not bars:
            print(f"[alpaca_client] WARNING: no bars returned for {sym}")
            continue
        df = pd.DataFrame(bars).rename(columns={
            "t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
        })
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
        out[sym] = df

    return out
