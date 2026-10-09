"""Upcoming earnings-date lookup, logged once at startup.

Alpaca has no earnings calendar, so this uses yfinance. It is best-effort
and informational only: any failure (package missing, network error, ETF
with no earnings) is logged and never affects trading.
"""
import logging
from datetime import date
from typing import Optional

log = logging.getLogger("earnings")


def next_earnings_date(symbol: str) -> Optional[date]:
    """Next earnings date on or after today, or None if unknown/not applicable."""
    import yfinance as yf  # lazy: optional dependency

    dates = yf.Ticker(symbol).calendar.get("Earnings Date") or []
    upcoming = sorted(d for d in dates if d >= date.today())
    return upcoming[0] if upcoming else None


def log_upcoming_earnings(symbols: list[str]) -> None:
    try:
        import yfinance  # noqa: F401
    except ImportError:
        log.warning("yfinance not installed — skipping earnings-date logging")
        return
    for sym in symbols:
        try:
            d = next_earnings_date(sym)
        except Exception as e:  # network/parse errors must not affect the bot
            log.warning("%s: earnings date lookup failed (%s)", sym, e)
            continue
        if d is None:
            log.info("%s: no upcoming earnings date (ETF or unknown)", sym)
        else:
            log.info("%s: next earnings %s (in %d days)", sym, d.isoformat(),
                     (d - date.today()).days)
