"""Historical OHLCV bars from TradingView, for backtesting only.

This uses tvDatafeed (https://github.com/rongardF/tvdatafeed), an
unofficial third-party client that pulls chart data over TradingView's
internal websocket API. It is not an official TradingView product, has
no SLA, and can break if TradingView changes that protocol - treat it as
an alternate data source for backtesting, never as a live trading feed.

Install (not a default dependency, since it's optional and git-installed):
    pip install git+https://github.com/rongardF/tvdatafeed.git

Anonymous (nologin) access is capped at ~5000 bars per request. Set
TV_USERNAME / TV_PASSWORD in .env for a TradingView account with broader
history access.
"""
import logging
import os
from datetime import timezone

log = logging.getLogger("tv_history")

_DEFAULT_EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]


def _interval_map():
    from tvDatafeed import Interval
    return {
        60: Interval.in_1_minute,
        180: Interval.in_3_minute,
        300: Interval.in_5_minute,
        900: Interval.in_15_minute,
        1800: Interval.in_30_minute,
        3600: Interval.in_1_hour,
        86400: Interval.in_daily,
    }


def _fetch_one(tv, symbol, exchange_hint, interval, n_bars):
    candidates = [exchange_hint] if exchange_hint else []
    candidates += [e for e in _DEFAULT_EXCHANGES if e not in candidates]
    for exch in candidates:
        try:
            df = tv.get_hist(symbol=symbol, exchange=exch,
                              interval=interval, n_bars=n_bars)
        except Exception as e:
            log.debug("tvDatafeed error for %s:%s (%s)", exch, symbol, e)
            continue
        if df is not None and not df.empty:
            log.info("Resolved %s to %s:%s (%d bars)", symbol, exch, symbol, len(df))
            return df
    return None


def fetch_history(symbols, timeframe_seconds, n_bars=5000,
                   exchange_overrides=None, username=None, password=None):
    """Fetch historical bars for `symbols` from TradingView.

    Returns {symbol: [bar, ...]} in the same shape backtest.py expects:
    {"ts": aware datetime, "open", "high", "low", "close", "volume"}.
    """
    try:
        from tvDatafeed import TvDatafeed
    except ImportError as e:
        raise ImportError(
            "tvDatafeed is not installed. Run:\n"
            "  pip install git+https://github.com/rongardF/tvdatafeed.git"
        ) from e

    interval = _interval_map().get(timeframe_seconds)
    if interval is None:
        raise ValueError(
            f"No TradingView interval for {timeframe_seconds}s bars; "
            f"supported seconds: {sorted(_interval_map())}"
        )

    exchange_overrides = exchange_overrides or {}
    username = username or os.getenv("TV_USERNAME")
    password = password or os.getenv("TV_PASSWORD")
    tv = TvDatafeed(username=username, password=password)

    bars_by_symbol = {}
    for symbol in symbols:
        df = _fetch_one(tv, symbol, exchange_overrides.get(symbol), interval, n_bars)
        if df is None:
            log.warning("No TradingView data for %s (tried %s)", symbol,
                        exchange_overrides.get(symbol) or _DEFAULT_EXCHANGES)
            continue
        bars_by_symbol[symbol] = [
            {
                # tvDatafeed builds these from a Unix epoch via
                # datetime.fromtimestamp(), i.e. a NAIVE local-time
                # datetime - astimezone(utc) reinterprets it as local
                # and converts, rather than mislabeling it UTC outright.
                "ts": ts.to_pydatetime().astimezone(timezone.utc),
                "open": float(row.open), "high": float(row.high),
                "low": float(row.low), "close": float(row.close),
                "volume": float(row.volume),
            }
            for ts, row in df.iterrows()
        ]
    return bars_by_symbol
