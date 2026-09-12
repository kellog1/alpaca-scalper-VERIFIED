"""Convert Webull's get_stock_bars MCP response into webull_swing's bar shape."""


def bars_from_webull(raw_result: list) -> dict:
    """`raw_result` is the `result` list from the Webull get_stock_bars tool:
    [{"symbol": ..., "result": [{"time","open","high","low","close","volume"}, ...]}, ...],
    each per-symbol list newest-first. Returns {symbol: [bar, ...]} sorted
    oldest-first with numeric fields cast to float and a "date" (YYYY-MM-DD)
    key used to match positions in state.json to a specific bar.
    """
    out = {}
    for entry in raw_result:
        symbol = entry["symbol"]
        bars = []
        for b in reversed(entry["result"]):
            bars.append({
                "date": b["time"][:10],
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b["volume"]),
            })
        out[symbol] = bars
    return out
