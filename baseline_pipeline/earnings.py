"""
Earnings-date annotation for the daily log (report-only).
---------------------------------------------------------
Adds `next_earnings` and `days_to_earnings` to each logged row so you can see
which basket names carry earnings-gap risk (a gap can open beyond an ATR stop,
and a stop order then fills at the open, worse than the stop price).

This does NOT change sizing, stops or orders. Dates come from the checked-in
`earnings_dates.csv` (symbol,next_earnings,source,as_of): there is no free
earnings feed on Alpaca, and a live feed would need another API key. Keep the
file current by hand; a date in the past is treated as unknown and warned about.
Blank next_earnings means "none expected" (ETFs).
"""
from __future__ import annotations
from datetime import date
from pathlib import Path
import pandas as pd

EARNINGS_PATH = Path(__file__).with_name("earnings_dates.csv")
WARN_WITHIN_DAYS = 3


def load_earnings(path: Path | str = EARNINGS_PATH) -> dict[str, date | None]:
    """{symbol: next earnings date or None}. A missing file means no annotations."""
    p = Path(path)
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype=str).fillna("")
    out: dict[str, date | None] = {}
    for _, r in df.iterrows():
        raw = r["next_earnings"].strip()
        try:
            out[r["symbol"].strip().upper()] = date.fromisoformat(raw) if raw else None
        except ValueError:
            out[r["symbol"].strip().upper()] = None   # unparsable counts as unknown
    return out


def annotate_earnings(detail: pd.DataFrame, earnings: dict[str, date | None], today: date) -> tuple[pd.DataFrame, list[str]]:
    """Returns (detail + next_earnings/days_to_earnings columns, console notes).

    next_earnings is an ISO date or empty; days_to_earnings is an int or empty.
    Symbols not in the file, ETFs, and past (stale) dates all log as empty;
    only stale dates and imminent reports (<= WARN_WITHIN_DAYS) produce notes.
    """
    notes: list[str] = []
    nxt, days = [], []
    for sym in detail["symbol"] if len(detail) else []:
        d = earnings.get(str(sym).upper())
        if d is None:
            nxt.append(""); days.append("")
            continue
        n = (d - today).days
        if n < 0:
            nxt.append(""); days.append("")
            notes.append(f"earnings_dates.csv is stale for {sym} (listed {d}, already past) - update it")
            continue
        nxt.append(d.isoformat()); days.append(n)
        if n <= WARN_WITHIN_DAYS:
            notes.append(f"EARNINGS SOON: {sym} reports {d} (in {n} day{'s' if n != 1 else ''}); gap risk past its stop")
    out = detail.copy()
    out["next_earnings"] = nxt
    out["days_to_earnings"] = days
    return out, notes
