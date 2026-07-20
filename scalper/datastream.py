"""Real-time data: Alpaca WebSocket trades aggregated into 30-second bars.

- Subscribes to raw trades for all configured symbols.
- Aggregates into fixed 30s bars (Alpaca's stream provides 1-min bars natively,
  so sub-minute bars are built client-side from ticks).
- Auto-reconnects with exponential backoff on disconnect.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from alpaca.data.live import StockDataStream

log = logging.getLogger("stream")

BarCallback = Callable[[str, dict], Awaitable[None]]
TickCallback = Callable[[str, float], Awaitable[None]]


class BarAggregator:
    """Aggregates trade ticks into fixed-interval bars per symbol."""

    def __init__(self, interval_seconds: int, on_bar: BarCallback):
        self.interval = interval_seconds
        self.on_bar = on_bar
        self.current: dict[str, dict] = {}

    def _bucket(self, ts: datetime) -> int:
        return int(ts.timestamp() // self.interval)

    async def on_trade(self, symbol: str, price: float, size: float, ts: datetime):
        bucket = self._bucket(ts)
        bar = self.current.get(symbol)
        if bar is None or bar["bucket"] != bucket:
            if bar is not None:
                await self.on_bar(symbol, bar)  # close out previous bar
            self.current[symbol] = {
                "bucket": bucket, "ts": ts,
                "open": price, "high": price, "low": price,
                "close": price, "volume": size,
            }
        else:
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += size


class DataStream:
    def __init__(self, api_key: str, secret_key: str, symbols: list[str],
                 bar_seconds: int, on_bar: BarCallback, on_tick: TickCallback):
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbols = symbols
        self.aggregator = BarAggregator(bar_seconds, on_bar)
        self.on_tick = on_tick
        self._stop = False

    async def _handle_trade(self, trade):
        ts = trade.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        await self.aggregator.on_trade(trade.symbol, trade.price, trade.size, ts)
        await self.on_tick(trade.symbol, trade.price)

    async def run_forever(self):
        """Run the stream with automatic reconnection + backoff."""
        backoff = 1
        while not self._stop:
            stream = StockDataStream(self.api_key, self.secret_key)
            stream.subscribe_trades(self._handle_trade, *self.symbols)
            try:
                log.info("Connecting WebSocket for %d symbols...", len(self.symbols))
                await stream._run_forever()  # runs until disconnect/error
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Stream disconnected (%s). Reconnecting in %ds...",
                            e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    await stream.close()
                except Exception:
                    pass

    def stop(self):
        self._stop = True
